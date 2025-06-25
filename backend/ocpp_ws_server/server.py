# OCPP 1.6 WebSocket сервер
import asyncio
from websockets.server import serve
from ocpp.v16 import ChargePoint as CP
from ocpp.routing import on
from ocpp.v16 import call_result
from ocpp_ws_server.redis_manager import redis_manager
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.db.session import get_session_local
from app.crud.ocpp import get_charging_session, update_charging_session, list_tariffs
from datetime import datetime

# --- Хранилище активных сессий и лимитов ---
active_sessions = {}  # station_id: {session_id, energy_limit, energy_delivered}

class ChargePoint(CP):
    @on('BootNotification')
    async def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        print(f"BootNotification from {self.id}: {charge_point_model}, {charge_point_vendor}")
        return call_result.BootNotification(
            current_time='2024-06-01T12:00:00Z',
            interval=10,
            status='Accepted'
        )

    @on('Heartbeat')
    async def on_heartbeat(self, **kwargs):
        print(f"Heartbeat from {self.id}")
        return call_result.Heartbeat(current_time=datetime.utcnow().isoformat())

    @on('StartTransaction')
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        print(f"StartTransaction from {self.id}: connector {connector_id}, id_tag {id_tag}, meter_start {meter_start}, timestamp {timestamp}")
        # Сохраняем стартовые данные сессии
        session = active_sessions.get(self.id, {})
        session['meter_start'] = meter_start
        session['energy_delivered'] = 0.0
        # Генерируем и сохраняем transaction_id
        transaction_id = int(datetime.utcnow().timestamp())
        session['transaction_id'] = transaction_id
        active_sessions[self.id] = session
        transaction = {
            "station_id": self.id,
            "type": "start",
            "connector_id": connector_id,
            "id_tag": id_tag,
            "meter_start": meter_start,
            "timestamp": timestamp,
            "created_at": datetime.utcnow().isoformat(),
            "transaction_id": transaction_id
        }
        await redis_manager.add_transaction(self.id, transaction)
        return call_result.StartTransaction(
            transaction_id=transaction_id,
            id_tag_info={"status": "Accepted"}
        )

    @on('StopTransaction')
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id, id_tag, **kwargs):
        print(f"StopTransaction from {self.id}: meter_stop {meter_stop}, transaction_id {transaction_id}, id_tag {id_tag}, timestamp {timestamp}")
        session_info = active_sessions.get(self.id)
        if self.id in active_sessions:
            del active_sessions[self.id]
        transaction = {
            "station_id": self.id,
            "type": "stop",
            "transaction_id": transaction_id,
            "id_tag": id_tag,
            "meter_stop": meter_stop,
            "timestamp": timestamp,
            "created_at": datetime.utcnow().isoformat()
        }
        await redis_manager.add_transaction(self.id, transaction)
        # --- Обновление сессии в БД (упрощенная версия) ---
        if session_info and session_info.get('session_id'):
            session_id = session_info['session_id']
            try:
                SessionLocal = get_session_local()
                db = SessionLocal()
                charging_session = get_charging_session(db, session_id)
                if charging_session:
                    meter_start = session_info.get('meter_start', 0.0)
                    energy_delivered = float(meter_stop) - float(meter_start)
                    tariffs = list_tariffs(db, charging_session.station_id)
                    tariff = tariffs[0] if tariffs else None
                    amount = energy_delivered * tariff.price_per_kwh if tariff else 0.0
                    # Обновляем сессию
                    update_charging_session(db, session_id, {
                            'energy': energy_delivered,
                            'amount': amount,
                            'status': 'stopped',
                            'stop_time': datetime.utcnow()
                        })
                    db.commit()
            except Exception as e:
                print(f"[DB ERROR] Ошибка при обновлении ChargingSession: {e}")
            finally:
                db.close()
        return call_result.StopTransaction(
            id_tag_info={"status": "Accepted"}
        )

    @on('MeterValues')
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        # Обработка показаний счетчика для контроля лимита
        print(f"MeterValues from {self.id}: {meter_value}")
        session = active_sessions.get(self.id)
        if not session:
            return
        try:
            value = meter_value[0]['sampledValue'][0]['value']
            value = float(value)
        except Exception:
            return
        meter_start = session.get('meter_start', 0.0)
        energy_delivered = value - meter_start
        session['energy_delivered'] = energy_delivered
        energy_limit = session.get('energy_limit')
        # --- Автоматическая остановка при достижении лимита ---
        if energy_limit and energy_delivered >= energy_limit:
            print(f"Достигнут лимит энергии {energy_delivered} >= {energy_limit}, инициируем StopTransaction!")
            await redis_manager.publish_command(self.id, {"command": "RemoteStopTransaction"})
        active_sessions[self.id] = session

async def handle_pubsub_commands(charge_point, station_id):
    async for command in redis_manager.listen_commands(station_id):
        print(f"Получена команда для {station_id}: {command}")
        if command.get("command") == "RemoteStartTransaction":
            payload = command.get("payload", {})
            session_id = payload.get("session_id")
            energy_limit = payload.get("energy_limit")
            active_sessions[station_id] = {
                "session_id": session_id,
                "energy_limit": energy_limit,
                "energy_delivered": 0.0
            }
            response = await charge_point.call("RemoteStartTransaction", **payload)
            print(f"Ответ на RemoteStartTransaction: {response}")
        elif command.get("command") == "RemoteStopTransaction":
            print(f"RemoteStopTransaction для {station_id}")
            session = active_sessions.get(station_id, {})
            transaction_id = session.get('transaction_id', 1)
            await charge_point.call("StopTransaction", 
                                   transaction_id=transaction_id, 
                                   meter_stop=0, 
                                   timestamp=datetime.utcnow().isoformat(), 
                                   id_tag="system")

async def handler(websocket):
    # Получаем cp_id из пути подключения
    # Поддержка путей /ws/{cp_id} и /ocpp/{cp_id}
    path_parts = websocket.path.strip('/').split('/')
    if len(path_parts) >= 2 and path_parts[0] in ['ws', 'ocpp']:
        cp_id = path_parts[1]
    else:
        cp_id = path_parts[-1]
    
    # Обработка X-Forwarded-For для получения реального IP за прокси
    real_ip = websocket.remote_address[0]
    if hasattr(websocket, 'request_headers'):
        x_forwarded_for = websocket.request_headers.get('X-Forwarded-For')
        if x_forwarded_for:
            real_ip = x_forwarded_for.split(',')[0].strip()
    
    print(f"Новое подключение: {cp_id} с IP: {real_ip}")
    charge_point = ChargePoint(cp_id, websocket)
    await redis_manager.register_station(cp_id)
    pubsub_task = asyncio.create_task(handle_pubsub_commands(charge_point, cp_id))
    try:
        await charge_point.start()
    finally:
        pubsub_task.cancel()
        await redis_manager.unregister_station(cp_id)
        print(f"Станция отключена: {cp_id}")

async def main():
    # Получаем порт из переменной окружения
    ws_port = int(os.getenv("OCPP_WS_PORT", "80"))
    async with serve(handler, "0.0.0.0", ws_port, subprotocols=["ocpp1.6"]):
        print(f"======== Running on ws://0.0.0.0:{ws_port}/ocpp/{{cp_id}} ========")
        await asyncio.Future()  # run forever

if __name__ == '__main__':
    asyncio.run(main()) 