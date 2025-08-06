"""
API v1 модули
"""
from fastapi import APIRouter

# Импортируем роутеры из модулей
from .charging import start_router, stop_router, status_router

# Создаем общий роутер для v1
router = APIRouter(prefix="/api/v1")

# Подключаем модули
router.include_router(start_router, tags=["charging"])
router.include_router(stop_router, tags=["charging"])
router.include_router(status_router, tags=["charging"])

__all__ = ["router"]