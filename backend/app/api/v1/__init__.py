"""
API v1 модули
"""
from fastapi import APIRouter

# Импортируем роутеры из модулей
from .charging import start_router, stop_router, status_router
from . import balance, payment, station, locations, notifications
from .auth import session as auth_session
from . import profile as profile_module
from . import history as history_module
from . import favorites as favorites_module

# Создаем общий роутер для v1
router = APIRouter(prefix="/api/v1")

# Подключаем модули
router.include_router(start_router, tags=["charging"])
router.include_router(stop_router, tags=["charging"])
router.include_router(status_router, tags=["charging"])

# Подключаем новые модульные маршруты
router.include_router(balance.router, tags=["balance"])
router.include_router(payment.router, tags=["payment"])
router.include_router(station.router, tags=["station"])
router.include_router(locations.router, tags=["locations"])
router.include_router(profile_module.router, tags=["profile"])
router.include_router(notifications.router)  # Push Notifications 
router.include_router(auth_session.router, tags=["auth"])
router.include_router(history_module.router, tags=["history"])
router.include_router(favorites_module.router, tags=["favorites"])

__all__ = ["router"]