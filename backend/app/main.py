from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import uvicorn
import os
import asyncio
from datetime import datetime
from sqlalchemy import text

from app.core.config import settings
from ocpp_ws_server.ws_handler import OCPPWebSocketHandler
from ocpp_ws_server.redis_manager import redis_manager
from app.api import mobile  # Импорт mobile API
from app.crud.ocpp_service import payment_lifecycle_service
from app.db.session import get_db

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# BACKGROUND TASKS ДЛЯ ПЛАТЕЖНОЙ СИСТЕМЫ
# ============================================================================

async def payment_status_checker():
    """Background task для периодической проверки статусов платежей"""
    while True:
        try:
            logger.info("🔍 Запуск проверки статусов платежей...")
            
            # Получаем активные платежи для проверки
            with next(get_db()) as db:
                # Проверяем пополнения баланса
                try:
                    pending_topups = db.execute(text("""
                        SELECT invoice_id FROM balance_topups 
                        WHERE status = 'pending' 
                        AND needs_status_check = true 
                        AND invoice_expires_at > NOW()
                        LIMIT 50
                    """)).fetchall()
                except UnicodeDecodeError as e:
                    logger.error(f"Unicode error in topups query, skipping: {e}")
                    pending_topups = []
                
                for (invoice_id,) in pending_topups:
                    try:
                        await payment_lifecycle_service.perform_status_check(
                            db, "balance_topups", invoice_id
                        )
                        await asyncio.sleep(0.5)  # Пауза между запросами к O!Dengi
                    except Exception as e:
                        logger.error(f"Status check failed for topup {invoice_id}: {e}")
                
                # Проверяем платежи за зарядку
                try:
                    pending_charging = db.execute(text("""
                        SELECT invoice_id FROM charging_payments 
                        WHERE status = 'pending' 
                        AND needs_status_check = true 
                        AND invoice_expires_at > NOW()
                        LIMIT 50
                    """)).fetchall()
                except UnicodeDecodeError as e:
                    logger.error(f"Unicode error in charging query, skipping: {e}")
                    pending_charging = []
                
                for (invoice_id,) in pending_charging:
                    try:
                        await payment_lifecycle_service.perform_status_check(
                            db, "charging_payments", invoice_id
                        )
                        await asyncio.sleep(0.5)  # Пауза между запросами к O!Dengi
                    except Exception as e:
                        logger.error(f"Status check failed for charging {invoice_id}: {e}")
                
                logger.info(f"✅ Проверка завершена: {len(pending_topups)} пополнений, {len(pending_charging)} платежей за зарядку")
        
        except Exception as e:
            logger.error(f"Payment status checker error: {e}")
        
        # Ждем 60 секунд до следующей проверки
        await asyncio.sleep(60)

async def payment_cleanup_task():
    """Background task для очистки просроченных платежей"""
    while True:
        try:
            logger.info("🧹 Запуск очистки просроченных платежей...")
            
            with next(get_db()) as db:
                result = await payment_lifecycle_service.cleanup_expired_payments(db)
                if result.get("success"):
                    logger.info(f"✅ Очистка завершена: отменено {result.get('cancelled_topups', 0)} пополнений, {result.get('cancelled_charging_payments', 0)} платежей за зарядку")
        
        except Exception as e:
            logger.error(f"Payment cleanup error: {e}")
        
        # Ждем 5 минут до следующей очистки
        await asyncio.sleep(300)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager для приложения"""
    logger.info("🚀 Starting OCPP WebSocket Server...")
    logger.info("✅ Redis manager initialized")
    
    # Запуск background tasks для платежной системы
    payment_checker_task = asyncio.create_task(payment_status_checker())
    payment_cleanup_task_ref = asyncio.create_task(payment_cleanup_task())
    logger.info("🔍 Payment status checker started")
    logger.info("🧹 Payment cleanup task started")
    
    yield
    
    # Отмена background tasks при остановке
    payment_checker_task.cancel()
    payment_cleanup_task_ref.cancel()
    logger.info("🛑 Shutting down OCPP WebSocket Server...")
    logger.info("✅ Application shutdown complete")

# Создание FastAPI приложения
app = FastAPI(
    title="EvPower OCPP WebSocket Server",
    description="Минимальный OCPP 1.6 WebSocket сервер только для ЭЗС. HTTP endpoints реализованы в FlutterFlow.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,  # Отключаем Swagger UI
    redoc_url=None  # Отключаем ReDoc
)

# CORS настройки (минимальные)
allowed_origins = os.getenv("ALLOWED_HOSTS", "").split(",")
if not allowed_origins or allowed_origins == [""]:
    allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ============================================================================
# ПОДКЛЮЧЕНИЕ API РОУТЕРОВ
# ============================================================================

# Mobile API для FlutterFlow
app.include_router(mobile.router)

# ============================================================================
# HEALTH CHECK ENDPOINT (единственный HTTP endpoint)
# ============================================================================

@app.get("/health", summary="Проверка здоровья OCPP сервера")
async def health_check():
    """Проверка состояния OCPP WebSocket сервера"""
    try:
        redis_status = await redis_manager.ping()
        if not redis_status:
            raise Exception("Redis недоступен - OCPP функции не работают")
            
        connected_stations = await redis_manager.get_stations()
        return {
            "status": "healthy",
            "service": "EvPower OCPP WebSocket Server",
            "version": "1.0.0",
            "redis": "connected",
            "connected_stations": len(connected_stations),
            "endpoints": ["ws://{host}/ws/{station_id}", "GET /health"],
            "note": "Все системы работают"
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "service": "EvPower OCPP WebSocket Server", 
            "version": "1.0.0",
            "error": str(e),
            "redis": "disconnected",
            "note": "КРИТИЧЕСКАЯ ОШИБКА: Redis недоступен - OCPP и зарядка не работают!"
        }

# ============================================================================
# OCPP WEBSOCKET ENDPOINT (основная функциональность)
# ============================================================================

@app.websocket("/ws/{station_id}")
async def websocket_endpoint(websocket: WebSocket, station_id: str):
    """
    WebSocket endpoint для подключения зарядных станций по протоколу OCPP 1.6
    
    Поддерживаемые OCPP сообщения:
    - BootNotification
    - Heartbeat
    - StatusNotification
    - Authorize
    - StartTransaction
    - StopTransaction
    - MeterValues
    """
    handler = OCPPWebSocketHandler(station_id, websocket)
    try:
        await handler.handle_connection()
    except WebSocketDisconnect:
        logger.info(f"Station {station_id} disconnected")
    except Exception as e:
        logger.error(f"WebSocket error for station {station_id}: {e}")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)

# ============================================================================
# ROOT ENDPOINT
# ============================================================================

@app.get("/", summary="Информация о сервере")
async def root():
    """Корневой endpoint с информацией о сервере"""
    return {
        "service": "EvPower OCPP WebSocket Server",
        "description": "Минимальный OCPP 1.6 WebSocket сервер для зарядных станций",
        "websocket_url": "ws://{host}/ws/{station_id}",
        "health_check": "GET /health",
        "version": "1.0.0",
        "protocol": "OCPP 1.6 JSON",
        "note": "HTTP API endpoints реализованы отдельно в FlutterFlow проекте"
    }

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )

