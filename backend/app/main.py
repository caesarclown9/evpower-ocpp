from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import uvicorn
import os

from app.core.config import settings
from ocpp_ws_server.ws_handler import OCPPWebSocketHandler
from ocpp_ws_server.redis_manager import redis_manager
from app.api import mobile  # Импорт mobile API

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager для приложения"""
    logger.info("🚀 Starting OCPP WebSocket Server...")
    logger.info("✅ Redis manager initialized") 
    yield
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
        connected_stations = await redis_manager.get_stations()
        return {
            "status": "healthy",
            "service": "EvPower OCPP WebSocket Server",
            "version": "1.0.0",
            "redis": "connected" if redis_status else "disconnected",
            "connected_stations": len(connected_stations),
            "endpoints": ["ws://{host}/ws/{station_id}", "GET /health"],
            "note": "HTTP endpoints реализованы в FlutterFlow"
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "error": str(e),
            "redis": "error"
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

