"""
API v1 модули
"""
from fastapi import APIRouter

# Импортируем роутеры из модулей
from .charging import start_router, stop_router, status_router
from . import balance, payment, station, locations
from . import profile as profile_module

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

__all__ = ["router"]