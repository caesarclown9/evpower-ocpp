import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from app.api import ocpp
from app.core.config import settings
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
import logging
from ocpp.v16 import ChargePoint as CP
from ocpp.routing import on
from ocpp.v16 import call_result
from ocpp_ws_server.redis_manager import redis_manager
from app.db.session import SessionLocal
from app.crud.ocpp import get_charging_session, update_charging_session, calculate_charging_cost
from datetime import datetime
import asyncio
from typing import Dict, Any
import traceback

# --- Импорт для автоматического создания полей ---
from app.db.base_class import Base
from app.db.session import engine
from app.db import models  # noqa: F401, чтобы зарегистрировать все модели
from app.crud.ocpp_service import (
    OCPPStationService,
    OCPPTransactionService, 
    OCPPMeterService,
    OCPPAuthorizationService,
    OCPPConfigurationService
)
from app.db.models.stations import ChargingSession

# Импорт API роутеров
from app.api.ocpp_endpoints import router as ocpp_router

# Конфигурация логирования
log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
log_handlers = [logging.StreamHandler()]

# Добавляем файловый хендлер только если путь существует или может быть создан
try:
    os.makedirs(settings.LOG_PATH, exist_ok=True)
    log_handlers.append(logging.FileHandler(f'{settings.LOG_PATH}/app.log', encoding='utf-8'))
except (OSError, PermissionError):
    # Если не можем создать директорию логов, используем только консоль
    pass

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(__name__)

# Создание FastAPI приложения
app = FastAPI(
    title="OCPP WebSocket Server API",
    version="1.0.0",
    description="Production OCPP 1.6 сервер для управления зарядными станциями",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Middleware для безопасности
allowed_hosts = settings.ALLOWED_HOSTS.split(",") if settings.ALLOWED_HOSTS else ["*"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

# CORS настройки для production
cors_origins = settings.CORS_ORIGINS.split(",") if settings.CORS_ORIGINS else []
if cors_origins and cors_origins[0]:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

# Для development добавляем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение OCPP роутера
app.include_router(ocpp.router)

# Подключение роутеров
app.include_router(ocpp_router, prefix="/api/v1/ocpp", tags=["OCPP"])

# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check для мониторинга"""
    try:
        # Проверка подключения к базе данных
        db = SessionLocal()
        db.execute("SELECT 1")
        db.close()
        
        # Проверка Redis
        station_count = len(await redis_manager.get_stations())
        
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "connected_stations": station_count,
            "database": "connected",
            "redis": "connected"
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "timestamp": datetime.utcnow().isoformat(),
            "error": str(e)
        }

# --- Автоматическое создание полей в таблицах ---
@app.on_event("startup")
async def on_startup():
    """Инициализация при запуске"""
    logger.info("STARTUP: Запуск OCPP сервера...")
    
    try:
        # Создание таблиц БД
        with engine.begin() as conn:
            Base.metadata.create_all(bind=conn)
            logger.info("SUCCESS: База данных инициализирована")
        
        # Создание директории для логов
        try:
            os.makedirs(settings.LOG_PATH, exist_ok=True)
        except (OSError, PermissionError):
            logger.warning(f"WARNING: Не удалось создать директорию логов: {settings.LOG_PATH}")
        
        logger.info("SUCCESS: OCPP сервер успешно запущен")
    except Exception as e:
        logger.error(f"ERROR: Ошибка при запуске: {e}")
        raise

@app.on_event("shutdown")
async def on_shutdown():
    """Очистка при остановке"""
    logger.info("SHUTDOWN: Остановка OCPP сервера...")

# --- Хранилище активных сессий и лимитов ---
active_sessions: Dict[str, Dict[str, Any]] = {}

class WebSocketAdapter:
    """Адаптер для совместимости FastAPI WebSocket с библиотекой ocpp"""
    
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
    
    async def recv(self):
        """Метод recv для совместимости с ocpp библиотекой"""
        return await self.websocket.receive_text()
    
    async def send(self, message):
        """Метод send для совместимости с ocpp библиотекой"""
        await self.websocket.send_text(message)
    
    async def close(self):
        """Закрытие соединения"""
        await self.websocket.close()

class ChargePoint(CP):
    """Production OCPP ChargePoint с полной интеграцией БД Phase 1"""
    
    def __init__(self, id: str, connection):
        super().__init__(id, connection)
        self.logger = logging.getLogger(f"ChargePoint.{id}")
        
    @on('BootNotification')
    def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        self.logger.info(f"BOOT: BootNotification: {charge_point_model}, {charge_point_vendor}")
        
        try:
            db = SessionLocal()
            
            # Проверяем существует ли станция в БД
            station = db.query(Station).filter(Station.id == self.id).first()
            if not station:
                # Создаем новую станцию если не существует
                station = Station(
                    id=self.id,
                    serial_number=self.id,
                    model=charge_point_model,
                    manufacturer=charge_point_vendor,
                    location_id="default",  # TODO: настроить через конфигурацию
                    power_capacity=22.0,    # TODO: получать из конфигурации
                    connector_types=["Type2"],
                    status="active",
                    admin_id="system"
                )
                db.add(station)
                db.commit()
                self.logger.info(f"CREATED: New station {self.id} added to database")
            
            # Обновляем статус OCPP станции
            firmware_version = kwargs.get('firmware_version')
            OCPPStationService.mark_boot_notification_sent(
                db, self.id, firmware_version
            )
            
            # Устанавливаем базовую конфигурацию
            OCPPConfigurationService.set_configuration(
                db, self.id, "HeartbeatInterval", "300", readonly=True
            )
            OCPPConfigurationService.set_configuration(
                db, self.id, "MeterValueSampleInterval", "60", readonly=True
            )
            
            db.close()
            
            return call_result.BootNotification(
                current_time=datetime.utcnow().isoformat() + 'Z',
                interval=300,  # 5 минут для production
                status='Accepted'
            )
            
        except Exception as e:
            self.logger.error(f"ERROR: Error in BootNotification: {e}")
            return call_result.BootNotification(
                current_time=datetime.utcnow().isoformat() + 'Z',
                interval=300,
                status='Rejected'
            )

    @on('Heartbeat')
    def on_heartbeat(self, **kwargs):
        self.logger.debug(f"HEARTBEAT: Heartbeat from {self.id}")
        
        try:
            db = SessionLocal()
            
            # Обновляем heartbeat в БД
            OCPPStationService.update_heartbeat(db, self.id)
            
            db.close()
            
            return call_result.Heartbeat(current_time=datetime.utcnow().isoformat())
            
        except Exception as e:
            self.logger.error(f"ERROR: Error in Heartbeat: {e}")
            return call_result.Heartbeat(current_time=datetime.utcnow().isoformat())

    @on('StatusNotification')
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        self.logger.info(f"STATUS: StatusNotification: connector={connector_id}, status={status}, error={error_code}")
        
        try:
            db = SessionLocal()
            
            # Обновляем статус станции
            info = kwargs.get('info')
            vendor_id = kwargs.get('vendor_id')
            vendor_error_code = kwargs.get('vendor_error_code')
            
            OCPPStationService.update_station_status(
                db, self.id, status, error_code, info, vendor_id, vendor_error_code
            )
            
            db.close()
            
            return call_result.StatusNotification()
            
        except Exception as e:
            self.logger.error(f"ERROR: Error in StatusNotification: {e}")
            return call_result.StatusNotification()

    @on('Authorize')
    def on_authorize(self, id_tag, **kwargs):
        self.logger.info(f"AUTH: Authorize request for id_tag: {id_tag}")
        
        try:
            db = SessionLocal()
            
            # Проверяем авторизацию
            auth_result = OCPPAuthorizationService.authorize_id_tag(db, id_tag)
            
            db.close()
            
            self.logger.info(f"AUTH: Authorization result for {id_tag}: {auth_result['status']}")
            
            return call_result.Authorize(id_tag_info=auth_result)
            
        except Exception as e:
            self.logger.error(f"ERROR: Error in Authorize: {e}")
            return call_result.Authorize(id_tag_info={"status": "Invalid"})

    @on('StartTransaction')
    def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        self.logger.info(f"START: StartTransaction: connector={connector_id}, id_tag={id_tag}, meter_start={meter_start}")
        
        try:
            db = SessionLocal()
            
            # Проверяем авторизацию
            auth_result = OCPPAuthorizationService.authorize_id_tag(db, id_tag)
            if auth_result["status"] != "Accepted":
                self.logger.warning(f"START: Unauthorized id_tag: {id_tag}")
                return call_result.StartTransaction(
                    transaction_id=0,
                    id_tag_info=auth_result
                )
            
            # Генерируем transaction_id
            transaction_id = int(datetime.utcnow().timestamp())
            
            # Получаем user_id по id_tag
            user_id = OCPPAuthorizationService.get_user_by_id_tag(db, id_tag)
            
            # Создаем сессию зарядки
            charging_session = ChargingSession(
                id=f"session_{self.id}_{transaction_id}",
                user_id=user_id or "unknown",
                station_id=self.id,
                start_time=datetime.utcnow(),
                status="started",
                limit_type="none"
            )
            db.add(charging_session)
            db.flush()  # Получаем ID сессии
            
            # Создаем OCPP транзакцию
            transaction = OCPPTransactionService.start_transaction(
                db, self.id, transaction_id, connector_id, id_tag,
                float(meter_start), datetime.fromisoformat(timestamp.replace('Z', '')),
                charging_session.id
            )
            
            # Сохраняем в активные сессии для мониторинга
            session = active_sessions.get(self.id, {})
            session['meter_start'] = meter_start
            session['energy_delivered'] = 0.0
            session['transaction_id'] = transaction_id
            session['charging_session_id'] = charging_session.id
            active_sessions[self.id] = session
            
            db.commit()
            db.close()
            
            self.logger.info(f"SUCCESS: Transaction started: {transaction_id}")
            return call_result.StartTransaction(
                transaction_id=transaction_id,
                id_tag_info={"status": "Accepted"}
            )
            
        except Exception as e:
            self.logger.error(f"ERROR: Error in StartTransaction: {e}")
            if 'db' in locals():
                db.rollback()
                db.close()
            return call_result.StartTransaction(
                transaction_id=0,
                id_tag_info={"status": "Invalid"}
            )

    @on('StopTransaction')
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, id_tag=None, **kwargs):
        self.logger.info(f"STOP: StopTransaction: transaction_id={transaction_id}, meter_stop={meter_stop}")
        
        try:
            db = SessionLocal()
            
            # Завершаем OCPP транзакцию
            stop_reason = kwargs.get('reason', 'Local')
            transaction = OCPPTransactionService.stop_transaction(
                db, self.id, transaction_id, float(meter_stop),
                datetime.fromisoformat(timestamp.replace('Z', '')), stop_reason
            )
            
            if transaction and transaction.charging_session_id:
                # Обновляем сессию зарядки
                charging_session = db.query(ChargingSession).filter(
                    ChargingSession.id == transaction.charging_session_id
                ).first()
                
                if charging_session:
                    energy_delivered = float(meter_stop) - float(transaction.meter_start)
                    
                    # Рассчитываем стоимость через тариф станции
                    station = db.query(Station).filter(Station.id == self.id).first()
                    if station and station.price_per_kwh:
                        amount = energy_delivered * float(station.price_per_kwh)
                    else:
                        amount = 0.0
                    
                    charging_session.stop_time = datetime.utcnow()
                    charging_session.energy = energy_delivered
                    charging_session.amount = amount
                    charging_session.status = "stopped"
            
            # Очистка активных сессий
            if self.id in active_sessions:
                del active_sessions[self.id]
            
            db.commit()
            db.close()
            
            self.logger.info(f"SUCCESS: Transaction completed: {transaction_id}")
            return call_result.StopTransaction(id_tag_info={"status": "Accepted"})
            
        except Exception as e:
            self.logger.error(f"ERROR: Error in StopTransaction: {e}")
            if 'db' in locals():
                db.rollback()
                db.close()
            return call_result.StopTransaction(id_tag_info={"status": "Invalid"})

    @on('MeterValues')
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        self.logger.debug(f"METER: MeterValues from {self.id}: connector={connector_id}")
        
        try:
            db = SessionLocal()
            
            # Получаем активную транзакцию
            active_transaction = OCPPTransactionService.get_active_transaction(db, self.id)
            transaction_id = active_transaction.transaction_id if active_transaction else None
            
            # Парсим timestamp из первого meter_value
            timestamp_str = meter_value[0].get('timestamp')
            if timestamp_str:
                timestamp = datetime.fromisoformat(timestamp_str.replace('Z', ''))
            else:
                timestamp = datetime.utcnow()
            
            # Парсим sampled values
            sampled_values = []
            for mv in meter_value:
                for sample in mv.get('sampledValue', []):
                    sampled_values.append({
                        'measurand': sample.get('measurand', ''),
                        'value': sample.get('value'),
                        'unit': sample.get('unit', ''),
                        'context': sample.get('context', ''),
                        'format': sample.get('format', ''),
                        'location': sample.get('location', '')
                    })
            
            # Сохраняем показания счетчика
            OCPPMeterService.add_meter_values(
                db, self.id, connector_id, timestamp, sampled_values, transaction_id
            )
            
            # Проверяем лимиты для активной сессии
            session = active_sessions.get(self.id)
            if session and sampled_values:
                for sample in sampled_values:
                    if sample['measurand'] == 'Energy.Active.Import.Register':
                        try:
                            current_energy = float(sample['value'])
                            meter_start = session.get('meter_start', 0.0)
                            energy_delivered = current_energy - meter_start
                            session['energy_delivered'] = energy_delivered
                            
                            energy_limit = session.get('energy_limit')
                            if energy_limit and energy_delivered >= energy_limit:
                                self.logger.warning(f"LIMIT: Energy limit reached: {energy_delivered} >= {energy_limit}")
                                await redis_manager.publish_command(self.id, {"command": "RemoteStopTransaction"})
                            
                            active_sessions[self.id] = session
                            break
                        except (ValueError, TypeError):
                            continue
            
            db.close()
            
        except Exception as e:
            self.logger.error(f"ERROR: Error in MeterValues: {e}")
            if 'db' in locals():
                db.close()

async def handle_pubsub_commands(charge_point: ChargePoint, station_id: str):
    """Обработка команд из Redis с расширенным логированием"""
    logger.info(f"PUBSUB: Listening for commands for station {station_id}")
    
    try:
        async for command in redis_manager.listen_commands(station_id):
            logger.info(f"COMMAND: Command received for {station_id}: {command}")
            
            try:
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
                    logger.info(f"RESPONSE: RemoteStartTransaction response: {response}")
                    
                elif command.get("command") == "RemoteStopTransaction":
                    logger.info(f"STOP: RemoteStopTransaction for {station_id}")
                    session = active_sessions.get(station_id, {})
                    transaction_id = session.get('transaction_id', 1)
                    
                    await charge_point.call("StopTransaction", 
                                          transaction_id=transaction_id, 
                                          meter_stop=0, 
                                          timestamp=datetime.utcnow().isoformat(), 
                                          id_tag="system")
                    
            except Exception as e:
                logger.error(f"ERROR: Error processing command: {e}")
                logger.error(traceback.format_exc())
                
    except Exception as e:
        logger.error(f"ERROR: Error in pubsub handler: {e}")
        logger.error(traceback.format_exc())

@app.websocket("/ws/{station_id}")
async def ocpp_ws(websocket: WebSocket, station_id: str):
    """Production WebSocket endpoint с улучшенной обработкой ошибок"""
    client_ip = websocket.client.host if websocket.client else "unknown"
    logger.info(f"WS_CONNECT: New OCPP connection from {client_ip} for station {station_id}")
    
    try:
        await websocket.accept(subprotocol="ocpp1.6")
        websocket_adapter = WebSocketAdapter(websocket)
        charge_point = ChargePoint(station_id, websocket_adapter)
        await redis_manager.register_station(station_id)
        
        pubsub_task = asyncio.create_task(handle_pubsub_commands(charge_point, station_id))
        
        logger.info(f"REGISTERED: Station {station_id} connected and registered")
        await charge_point.start()
        
    except WebSocketDisconnect:
        logger.info(f"WS_DISCONNECT: Station {station_id} disconnected normally")
    except Exception as e:
        logger.error(f"ERROR: Error in WebSocket connection for {station_id}: {e}")
        logger.error(traceback.format_exc())
    finally:
        # Очистка ресурсов
        try:
            await redis_manager.unregister_station(station_id)
            if station_id in active_sessions:
                del active_sessions[station_id]
            if 'pubsub_task' in locals():
                pubsub_task.cancel()
            logger.info(f"CLEANUP: Cleaned up resources for station {station_id}")
        except Exception as e:
            logger.error(f"ERROR: Error during cleanup for {station_id}: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

