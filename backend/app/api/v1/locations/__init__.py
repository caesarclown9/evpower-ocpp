from .locations import router
from .websocket import router as ws_router

# Объединяем роутеры
router.include_router(ws_router)
# Совместимость с клиентами, ожидающими префикс /locations в пути WebSocket
router.include_router(ws_router, prefix="/locations")

__all__ = ["router"]