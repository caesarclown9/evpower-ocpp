# Принудительная загрузка .env файла для background задач
import os
from pathlib import Path

# Загружаем .env файл явно перед импортом settings
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                # Устанавливаем только если переменная еще не установлена
                if key not in os.environ:
                    os.environ[key] = value

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import uvicorn
import asyncio
from datetime import datetime
from sqlalchemy import text

from app.core.config import settings
from ocpp_ws_server.ws_handler import OCPPWebSocketHandler
from ocpp_ws_server.redis_manager import redis_manager
from app.api import mobile  # Импорт mobile API

# Настройка логирования - используем только console
import sys

# Простая настройка логирования только в консоль
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Настройка специфичных логгеров
logging.getLogger("OCPPHandler").setLevel(logging.INFO)
logging.getLogger("OCPP").setLevel(logging.INFO)
logging.getLogger("websockets").setLevel(logging.INFO)
logging.getLogger("fastapi").setLevel(logging.INFO)
logging.getLogger("uvicorn").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ============================================================================
# BACKGROUND TASKS ДЛЯ ПЛАТЕЖНОЙ СИСТЕМЫ
# ============================================================================

async def check_payment_status(payment_table: str, invoice_id: str, max_checks: int = 20):
    """
    Проверяет статус конкретного платежа до его завершения
    
    Args:
        payment_table: "balance_topups"
        invoice_id: ID платежа для проверки
        max_checks: Максимальное количество проверок (по умолчанию 20)
    """
    logger.info(f"🔍 Запуск мониторинга платежа {invoice_id} (таблица: {payment_table})")
    
    for check_number in range(1, max_checks + 1):
        try:
            # Ждем 15 секунд перед каждой проверкой
            await asyncio.sleep(15)
            
            # Проверяем статус платежа
            try:
                from app.db.session import get_session_local
                from app.crud.ocpp_service import payment_lifecycle_service
                
                SessionLocal = get_session_local()
                db = SessionLocal()
                
                result = await payment_lifecycle_service.perform_status_check(
                    db, payment_table, invoice_id
                )
                
                db.close()
                
                if result.get("success"):
                    new_status = result.get("new_status")
                    logger.info(f"🔍 Платеж {invoice_id}: проверка {check_number}/{max_checks}, статус: {new_status}")
                    
                    # Если платеж завершен - прекращаем мониторинг
                    if new_status in ['approved', 'canceled', 'refunded']:
                        logger.info(f"✅ Мониторинг платежа {invoice_id} завершен: {new_status}")
                        return
                else:
                    logger.warning(f"⚠️ Платеж {invoice_id}: ошибка проверки статуса")
                    
            except Exception as e:
                logger.error(f"❌ Ошибка проверки платежа {invoice_id}: {e}")
                
        except Exception as e:
            logger.error(f"❌ Критическая ошибка мониторинга платежа {invoice_id}: {e}")
            break
    
    logger.warning(f"⏰ Мониторинг платежа {invoice_id} завершен по таймауту ({max_checks} проверок)")

def start_payment_monitoring(payment_table: str, invoice_id: str, max_checks: int = 20):
    """
    Удобная функция для запуска мониторинга платежа из API endpoints
    
    Args:
        payment_table: "balance_topups"
        invoice_id: ID платежа для проверки
        max_checks: Максимальное количество проверок
    """
    asyncio.create_task(check_payment_status(payment_table, invoice_id, max_checks))
    logger.info(f"🔍 Запущен мониторинг платежа {invoice_id} (таблица: {payment_table})")

async def payment_cleanup_task():
    """Background task для периодической очистки просроченных платежей"""
    # Ждем 30 минут перед первым запуском
    await asyncio.sleep(1800)
    
    while True:
        try:
            logger.info("🧹 Запуск периодической очистки просроченных платежей...")
            
            try:
                # Создаем connection только в момент использования
                from app.db.session import get_session_local
                SessionLocal = get_session_local()
                db = SessionLocal()
                
                from app.crud.ocpp_service import payment_lifecycle_service
                result = await payment_lifecycle_service.cleanup_expired_payments(db)
                if result.get("success"):
                    cancelled_topups = result.get('cancelled_topups', 0)
                    if cancelled_topups > 0:
                        logger.info(f"✅ Очистка завершена: отменено {cancelled_topups} пополнений")
                    else:
                        logger.info("✅ Очистка завершена: просроченных платежей не найдено")
                        
            except Exception as e:
                logger.error(f"Failed to create database connection for cleanup: {e}")
            finally:
                try:
                    db.close()
                except:
                    pass
        
        except Exception as e:
            logger.error(f"Payment cleanup error: {e}")
        
        # Ждем 1 час до следующей очистки
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager для приложения"""
    logger.info("🚀 Starting OCPP WebSocket Server...")
    
    # 🔍 DEBUG: показываем все переменные окружения связанные с Redis
    import os
    logger.info(f"🔍 DEBUG - ENVIRONMENT CHECK:")
    logger.info(f"🔍 REDIS_URL from env: {os.getenv('REDIS_URL', 'NOT SET')}")
    logger.info(f"🔍 All Redis-related env vars:")
    for key, value in os.environ.items():
        if 'redis' in key.lower() or 'REDIS' in key:
            logger.info(f"🔍 {key} = {value}")
    
    # 🔍 DEBUG: тестируем Redis подключение
    logger.info("🔍 Testing Redis connection...")
    try:
        ping_result = await redis_manager.ping()
        if ping_result:
            logger.info("✅ Redis PING successful!")
        else:
            logger.error("❌ Redis PING failed!")
    except Exception as e:
        logger.error(f"❌ Redis connection test failed: {e}")
    
    logger.info("✅ Redis manager initialized")
    
    # Запуск только cleanup задачи (проверка статусов платежей теперь по событию)
    payment_cleanup_task_ref = asyncio.create_task(payment_cleanup_task())
    logger.info("🧹 Payment cleanup task started (1 час между проверками)")
    logger.info("🔍 Payment status checks будут запускаться при создании платежей")
    
    yield
    
    # Отмена background tasks при остановке
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

# CORS настройки для WebSocket и HTTP
allowed_origins = os.getenv("ALLOWED_HOSTS", "").split(",")
if not allowed_origins or allowed_origins == [""]:
    allowed_origins = ["*"]

# Добавляем поддержку WebSocket в CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
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
    import os
    try:
        # 🔍 DEBUG для health check
        redis_url = os.getenv('REDIS_URL', 'NOT SET')
        logger.info(f"🔍 HEALTH CHECK - REDIS_URL: {redis_url}")
        
        redis_status = await redis_manager.ping()
        logger.info(f"🔍 HEALTH CHECK - Redis ping result: {redis_status}")
        
        if not redis_status:
            raise Exception("Redis недоступен - OCPP функции не работают")
            
        connected_stations = await redis_manager.get_stations()
        logger.info(f"🔍 HEALTH CHECK - Connected stations: {len(connected_stations)}")
        
        return {
            "status": "healthy",
            "service": "EvPower OCPP WebSocket Server",
            "version": "1.0.0",
            "redis": "connected",
            "connected_stations": len(connected_stations),
            "endpoints": ["ws://{host}/ws/{station_id}", "ws://{host}/ocpp/{station_id}", "GET /health"],
            "note": "Все системы работают"
        }
    except Exception as e:
        logger.error(f"❌ HEALTH CHECK FAILED: {e}")
        logger.error(f"🔍 HEALTH CHECK - Current REDIS_URL: {os.getenv('REDIS_URL', 'NOT SET')}")
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
@app.websocket("/ocpp/{station_id}")
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
        "websocket_urls": ["ws://{host}/ws/{station_id}", "ws://{host}/ocpp/{station_id}"],
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
        port=9210,
        log_level="info"
    )

