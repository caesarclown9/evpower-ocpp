"""
Favorites API - управление избранными станциями пользователя.
"""
from fastapi import APIRouter

from .favorites import router as favorites_router

router = APIRouter(prefix="/favorites", tags=["favorites"])
router.include_router(favorites_router)

__all__ = ["router"]
