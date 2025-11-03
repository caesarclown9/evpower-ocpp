# Environment variables загружаются из Docker/Coolify напрямую
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import uvicorn
import asyncio
from datetime import datetime
from sqlalchemy import text

from app.core.config import settings
from app.core.logging_config import setup_logging
from app.core.security_middleware import SecurityMiddleware
from app.core.auth_middleware import AuthMiddleware
from app.core.idempotency_middleware import IdempotencyMiddleware
from app.core.payment_audit import PaymentAuditMiddleware
from ocpp_ws_server.ws_handler import OCPPWebSocketHandler
from ocpp_ws_server.redis_manager import redis_manager
from app.api import mobile  # Импорт mobile API (будет постепенно заменен)
from app.api.v1 import router as v1_router  # Новая модульная структура
from app.services.station_status_manager import StationStatusManager
from app.db.session import get_db
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Настройка улучшенного логирования
setup_logging()

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
    
    # Проверка Redis подключения
    logger.info("🔄 Initializing Redis connection...")
    try:
        ping_result = await redis_manager.ping()
        if ping_result:
            logger.info("✅ Redis connection established successfully")
        else:
            logger.error("❌ Redis connection failed")
    except Exception as e:
        logger.error(f"❌ Redis connection error: {e}")
    
    logger.info("✅ Redis manager initialized")
    
    # Запуск только cleanup задачи (проверка статусов платежей теперь по событию)
    payment_cleanup_task_ref = asyncio.create_task(payment_cleanup_task())
    logger.info("🧹 Payment cleanup task started (1 час между проверками)")
    logger.info("🔍 Payment status checks будут запускаться при создании платежей")
    
    # Запуск scheduler для обновления статусов станций
    scheduler = AsyncIOScheduler()
    
    async def update_station_statuses_job():
        """Фоновая задача для обновления статусов станций"""
        try:
            with next(get_db()) as db:
                result = StationStatusManager.update_all_station_statuses(db)
                if result["deactivated"] or result["activated"]:
                    logger.info(f"📊 Обновлены статусы станций: "
                              f"активировано {len(result['activated'])}, "
                              f"деактивировано {len(result['deactivated'])}")
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче обновления статусов: {e}")

    async def check_hanging_sessions_job():
        """Фоновая задача для автоматической остановки зависших сессий зарядки"""
        try:
            from app.api.v1.charging.service import ChargingService
            from app.db.session import get_session_local

            SessionLocal = get_session_local()
            db = SessionLocal()

            try:
                charging_service = ChargingService(db)
                result = await charging_service.check_and_stop_hanging_sessions(
                    redis_manager=redis_manager,
                    max_hours=12,  # Максимум 12 часов активной зарядки
                    connection_timeout_minutes=10  # Таймаут на подключение кабеля
                )

                if result.get("stopped_count", 0) > 0:
                    logger.warning(f"⚠️ Автоматически остановлено {result['stopped_count']} зависших сессий "
                                 f"({result.get('no_connection_sessions_found', 0)} без подключения, "
                                 f"{result.get('long_sessions_found', 0)} длинных)")
                    for session in result.get("sessions", []):
                        reason_text = "НЕТ ПОДКЛЮЧЕНИЯ" if session.get('reason') == 'no_connection' else "ДОЛГО"
                        logger.info(f"  - Сессия {session['session_id']} ({reason_text}): "
                                  f"{session.get('duration_minutes', 0):.0f}мин, "
                                  f"{session['energy_consumed']} кВт⋅ч, "
                                  f"возврат {session.get('refund_amount', 0)} сом")

                db.commit()
            except Exception as e:
                logger.error(f"Ошибка при проверке зависших сессий: {e}", exc_info=True)
                db.rollback()
            finally:
                db.close()

        except Exception as e:
            logger.error(f"Критическая ошибка в задаче проверки зависших сессий: {e}", exc_info=True)

    # Запускаем каждые 2 минуты (чаще чем heartbeat timeout для надежности)
    scheduler.add_job(
        update_station_statuses_job,
        'interval',
        minutes=2,
        id='update_station_statuses',
        name='Update Station Statuses',
        misfire_grace_time=30
    )

    # Проверка зависших сессий каждые 30 минут
    scheduler.add_job(
        check_hanging_sessions_job,
        'interval',
        minutes=30,
        id='check_hanging_sessions',
        name='Check and Stop Hanging Charging Sessions',
        misfire_grace_time=60
    )
    
    scheduler.start()
    logger.info("⏰ Scheduler запущен:")
    logger.info("  - Обновление статусов станций: каждые 2 минуты")
    logger.info("  - Проверка зависших сессий зарядки: каждые 30 минут")
    logger.info("    • Автоостановка без подключения: > 10 минут")
    logger.info("    • Автоостановка длинных сессий: > 12 часов")
    
    yield
    
    # Остановка scheduler
    scheduler.shutdown()
    
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
    docs_url=("/docs" if settings.ENABLE_SWAGGER else None),
    redoc_url=("/redoc" if settings.ENABLE_SWAGGER else None)
)

# CORS настройки для WebSocket и HTTP
allowed_origins = os.getenv("ALLOWED_HOSTS", "").split(",")
if not allowed_origins or allowed_origins == [""]:
    allowed_origins = ["*"]

# Подключаем аутентификацию и идемпотентность до бизнес-логики
app.add_middleware(AuthMiddleware)
app.add_middleware(IdempotencyMiddleware)

# Добавляем Security Middleware (заголовки, базовый rate limiting)
security_middleware = SecurityMiddleware()
app.middleware("http")(security_middleware)

# Добавляем Payment Audit Middleware
payment_audit_middleware = PaymentAuditMiddleware()
app.middleware("http")(payment_audit_middleware)

# Получаем CORS origins из настроек (берется из env переменной CORS_ORIGINS)
cors_origins = settings.CORS_ORIGINS.split(",") if settings.CORS_ORIGINS else []
cors_origins = [origin.strip() for origin in cors_origins if origin.strip()]  # Убираем пробелы и пустые значения

# Fail-safe: если CORS origins пусты в dev, используем localhost
if not cors_origins:
    if settings.APP_ENV == "development":
        cors_origins = ["http://localhost:3000", "http://localhost:9210"]
        logger.warning("⚠️ CORS_ORIGINS not set - using development defaults")
    else:
        raise ValueError("CORS_ORIGINS must be explicitly set in production/staging environment")

# Валидация: запрещаем wildcard в production с allow_credentials
if "*" in cors_origins:
    if settings.APP_ENV == "production":
        raise ValueError("CORS wildcard (*) not allowed in production with allow_credentials=True")
    logger.warning("⚠️ CORS wildcard (*) detected - should not be used in production")

logger.info(f"📋 CORS origins configured: {len(cors_origins)} origins")

# Явно задаем разрешенные заголовки (принцип наименьших привилегий)
allowed_headers = [
    "Authorization",
    "Content-Type",
    "X-Client-Id",
    "X-Client-Timestamp",
    "X-Client-Signature",
    "Idempotency-Key",
    "X-Correlation-ID"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],  # Только необходимые методы
    allow_headers=allowed_headers,  # Явный список вместо "*"
    expose_headers=["X-Correlation-ID"],
    max_age=86400  # 24 часа кэш для preflight запросов
)

# ============================================================================
# ПОДКЛЮЧЕНИЕ API РОУТЕРОВ
# ============================================================================

# Mobile API для FlutterFlow (legacy, постепенно мигрирует в v1)
app.include_router(mobile.router)

# V1 API - новая модульная структура
app.include_router(v1_router)

# ============================================================================
# HEALTH CHECK ENDPOINT (единственный HTTP endpoint)
# ============================================================================

@app.get("/health", summary="Проверка здоровья OCPP сервера")
async def health_check():
    """Проверка состояния OCPP WebSocket сервера"""
    try:
        redis_status = await redis_manager.ping()
        logger.info(f"Health check - Redis: {'connected' if redis_status else 'disconnected'}")

        if not redis_status:
            raise Exception("Redis недоступен - OCPP функции не работают")

        connected_stations = await redis_manager.get_stations()
        logger.info(f"Health check - Connected stations: {len(connected_stations)}")
        
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
        logger.error(f"❌ Health check failed: {e}")
        return {
            "status": "unhealthy",
            "service": "EvPower OCPP WebSocket Server",
            "version": "1.0.0",
            "error": str(e),
            "redis": "disconnected",
            "note": "КРИТИЧЕСКАЯ ОШИБКА: Redis недоступен - OCPP и зарядка не работают!"
        }

@app.get("/health-force", summary="Принудительная диагностика Redis")
async def health_check_force():
    """Принудительная диагностика с пересозданием Redis подключения"""
    from ocpp_ws_server.redis_manager import RedisOcppManager

    try:
        # Принудительно создаем новый Redis manager
        logger.info("🔄 Force check - Creating new Redis connection")

        # Создаем новый экземпляр для тестирования
        test_redis = RedisOcppManager()

        # Пытаемся подключиться
        ping_result = await test_redis.ping()
        logger.info(f"🔄 Force check - Redis ping: {ping_result}")

        if ping_result:
            # Тестируем операции
            await test_redis.redis.set("health_test", "ok", ex=10)
            test_value = await test_redis.redis.get("health_test")
            await test_redis.redis.delete("health_test")

            logger.info(f"🔄 Force check - Read/write test: {'OK' if test_value else 'FAILED'}")

            return {
                "status": "healthy",
                "service": "EvPower OCPP WebSocket Server (FORCE CHECK)",
                "version": "1.0.0",
                "redis": "connected",
                "redis_configured": True,
                "ping_result": ping_result,
                "rw_test": test_value.decode() if test_value else None,
                "note": "Принудительная проверка прошла успешно"
            }
        else:
            raise Exception("Redis ping failed")

    except Exception as e:
        logger.error(f"❌ Force check failed: {e}")
        return {
            "status": "unhealthy",
            "service": "EvPower OCPP WebSocket Server (FORCE CHECK)",
            "version": "1.0.0",
            "error": str(e),
            "redis": "disconnected",
            "redis_configured": bool(settings.REDIS_URL),
            "note": f"Принудительная проверка не удалась: {e}"
        }

# Readiness endpoint (зависимости готовы)
@app.get("/readyz", summary="Готовность сервера")
async def ready_check():
    try:
        redis_status = await redis_manager.ping()
        if not redis_status:
            raise Exception("Redis недоступен")
        return {"status": "ready"}
    except Exception as e:
        return {"status": "not_ready", "error": str(e)}

# ============================================================================
# OCPP WEBSOCKET ENDPOINT (основная функциональность)
# ============================================================================

@app.websocket("/ws/{station_id}")
@app.websocket("/ocpp/{station_id}")
@app.websocket("/ws/{station_id}/")
@app.websocket("/ocpp/{station_id}/")
@app.websocket("/ws/{station_id}/{rest_path:path}")
@app.websocket("/ocpp/{station_id}/{rest_path:path}")
async def websocket_endpoint(websocket: WebSocket, station_id: str, rest_path: str = ""):
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

