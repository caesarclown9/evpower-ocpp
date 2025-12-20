"""
Admin API модули
- users: управление пользователями (superadmin only)
- operators: управление операторами (admin/superadmin)
"""
from fastapi import APIRouter
from . import users
from . import operators

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(users.router)
router.include_router(operators.router)

__all__ = ["router"]
