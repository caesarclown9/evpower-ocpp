import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.openapi.utils import get_openapi
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
from app.crud.ocpp import get_charging_session, update_charging_session, list_tariffs
from datetime import datetime
import asyncio
import uvicorn
from typing import Dict, Any
import json
import traceback

# --- Импорт для автоматического создания полей ---
from app.db.base_class import Base
from app.db.session import engine
from app.db import models  # noqa: F401, чтобы зарегистрировать все модели

# Конфигурация логирования
log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
log_handlers = [logging.StreamHandler()]

# Добавляем файловый хендлер только если путь существует или может быть создан
try:
    os.makedirs(settings.LOG_PATH, exist_ok=True)
    log_handlers.append(logging.FileHandler(f'{settings.LOG_PATH}/app.log'))
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
else:
    # Для development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение OCPP роутера
app.include_router(ocpp.router)

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
    logger.info("🚀 Запуск OCPP сервера...")
    
    try:
        # Создание таблиц БД
    with engine.begin() as conn:
        Base.metadata.create_all(bind=conn)
        logger.info("✅ База данных инициализирована")
        
        # Создание директории для логов
        try:
            os.makedirs(settings.LOG_PATH, exist_ok=True)
        except (OSError, PermissionError):
            logger.warning(f"Не удалось создать директорию логов: {settings.LOG_PATH}")
        
        logger.info("✅ OCPP сервер успешно запущен")
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске: {e}")
        raise

@app.on_event("shutdown")
async def on_shutdown():
    """Очистка при остановке"""
    logger.info("🛑 Остановка OCPP сервера...")

# --- Хранилище активных сессий и лимитов ---
active_sessions: Dict[str, Dict[str, Any]] = {}

class ChargePoint(CP):
    """Production OCPP ChargePoint с расширенным логированием"""
    
    def __init__(self, id: str, connection):
        super().__init__(id, connection)
        self.logger = logging.getLogger(f"ChargePoint.{id}")
        
    @on('BootNotification')
    def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        self.logger.info(f"🔌 BootNotification: {charge_point_model}, {charge_point_vendor}")
        return call_result.BootNotificationPayload(
            current_time=datetime.utcnow().isoformat() + 'Z',
            interval=300,  # 5 минут для production
            status='Accepted'
        )

    @on('Heartbeat')
    def on_heartbeat(self, **kwargs):
        self.logger.debug(f"💓 Heartbeat from {self.id}")
        return call_result.HeartbeatPayload(current_time=datetime.utcnow().isoformat())

    @on('StartTransaction')
    def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        self.logger.info(f"▶️ StartTransaction: connector={connector_id}, id_tag={id_tag}, meter_start={meter_start}")
        
        try:
        session = active_sessions.get(self.id, {})
        session['meter_start'] = meter_start
        session['energy_delivered'] = 0.0
        transaction_id = int(datetime.utcnow().timestamp())
        session['transaction_id'] = transaction_id
        active_sessions[self.id] = session
            
            # Логирование в Redis для мониторинга
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
            
            self.logger.info(f"✅ Transaction started: {transaction_id}")
        return call_result.StartTransactionPayload(
            transaction_id=transaction_id,
            id_tag_info={"status": "Accepted"}
        )
        except Exception as e:
            self.logger.error(f"❌ Error in StartTransaction: {e}")
            return call_result.StartTransactionPayload(
                transaction_id=0,
                id_tag_info={"status": "Invalid"}
            )

    @on('StopTransaction')
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, id_tag, **kwargs):
        self.logger.info(f"⏹️ StopTransaction: transaction_id={transaction_id}, meter_stop={meter_stop}")
        
        try:
        session_info = active_sessions.get(self.id)
        if self.id in active_sessions:
            del active_sessions[self.id]
            
            # Обновление сессии в БД
        if session_info and session_info.get('session_id'):
            session_id = session_info['session_id']
                db = SessionLocal()
                try:
                charging_session = get_charging_session(db, session_id)
                if charging_session:
                    meter_start = session_info.get('meter_start', 0.0)
                    energy_delivered = float(meter_stop) - float(meter_start)
                    tariffs = list_tariffs(db, charging_session.station_id)
                    tariff = tariffs[0] if tariffs else None
                    amount = energy_delivered * tariff.price_per_kwh if tariff else 0.0
                        
                        update_charging_session(db, session_id, {
                            'energy': energy_delivered,
                            'amount': amount,
                            'status': 'stopped',
                            'stop_time': datetime.utcnow()
                        })
                        db.commit()
                        
                        self.logger.info(f"💰 Session updated: energy={energy_delivered}kWh, amount=${amount}")
            except Exception as e:
                    self.logger.error(f"❌ DB error in StopTransaction: {e}")
            finally:
                db.close()
            
        return call_result.StopTransactionPayload(
            id_tag_info={"status": "Accepted"}
        )
        except Exception as e:
            self.logger.error(f"❌ Error in StopTransaction: {e}")
            return call_result.StopTransactionPayload(
                id_tag_info={"status": "Invalid"}
            )

    @on('MeterValues')
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        self.logger.debug(f"📊 MeterValues from {self.id}: {meter_value}")
        
        try:
        session = active_sessions.get(self.id)
        if not session:
            return
                
            value = meter_value[0]['sampledValue'][0]['value']
            value = float(value)
        meter_start = session.get('meter_start', 0.0)
        energy_delivered = value - meter_start
        session['energy_delivered'] = energy_delivered
        energy_limit = session.get('energy_limit')
            
            # Автоматическая остановка при достижении лимита
        if energy_limit and energy_delivered >= energy_limit:
                self.logger.warning(f"⚠️ Energy limit reached: {energy_delivered} >= {energy_limit}")
            await redis_manager.publish_command(self.id, {"command": "RemoteStopTransaction"})
                
        active_sessions[self.id] = session
        except Exception as e:
            self.logger.error(f"❌ Error in MeterValues: {e}")

async def handle_pubsub_commands(charge_point: ChargePoint, station_id: str):
    """Обработка команд из Redis с расширенным логированием"""
    logger.info(f"📡 Listening for commands for station {station_id}")
    
    try:
    async for command in redis_manager.listen_commands(station_id):
            logger.info(f"📨 Command received for {station_id}: {command}")
            
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
                    logger.info(f"✅ RemoteStartTransaction response: {response}")
                    
        elif command.get("command") == "RemoteStopTransaction":
                    logger.info(f"⏹️ RemoteStopTransaction for {station_id}")
            session = active_sessions.get(station_id, {})
            transaction_id = session.get('transaction_id', 1)
                    
                    await charge_point.call("StopTransaction", 
                                           transaction_id=transaction_id, 
                                           meter_stop=0, 
                                           timestamp=datetime.utcnow().isoformat(), 
                                           id_tag="system")
                    
            except Exception as e:
                logger.error(f"❌ Error processing command: {e}")
                logger.error(traceback.format_exc())
                
    except Exception as e:
        logger.error(f"❌ Error in pubsub handler: {e}")

@app.websocket("/ws/{station_id}")
async def ocpp_ws(websocket: WebSocket, station_id: str):
    """Production WebSocket endpoint с улучшенной обработкой ошибок"""
    client_ip = websocket.client.host if websocket.client else "unknown"
    logger.info(f"🔌 New OCPP connection from {client_ip} for station {station_id}")
    
    try:
    await websocket.accept(subprotocol="ocpp1.6")
    charge_point = ChargePoint(station_id, websocket)
    await redis_manager.register_station(station_id)
        
    pubsub_task = asyncio.create_task(handle_pubsub_commands(charge_point, station_id))
        
        logger.info(f"✅ Station {station_id} connected and registered")
        await charge_point.start()
        
    except WebSocketDisconnect:
        logger.info(f"🔌 Station {station_id} disconnected normally")
    except Exception as e:
        logger.error(f"❌ Error in WebSocket connection for {station_id}: {e}")
        logger.error(traceback.format_exc())
    finally:
        try:
        pubsub_task.cancel()
        await redis_manager.unregister_station(station_id)
            logger.info(f"🔌 Station {station_id} cleanup completed")
        except Exception as e:
            logger.error(f"❌ Error during cleanup for {station_id}: {e}")

if __name__ == "__main__":
    # Настройки для development
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        reload=True,
        log_level="info"
    )

