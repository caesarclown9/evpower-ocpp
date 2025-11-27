"""
History API - история зарядок и транзакций пользователя.
"""
from fastapi import APIRouter

from .history import router as history_router

router = APIRouter(prefix="/history", tags=["history"])
router.include_router(history_router)

__all__ = ["router"]
