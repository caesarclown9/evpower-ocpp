from .locations import router
from .websocket import router as ws_router

# Объединяем роутеры
router.include_router(ws_router)

__all__ = ["router"]