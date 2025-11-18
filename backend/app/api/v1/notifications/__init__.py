"""
Push Notifications API Module
"""
from fastapi import APIRouter
from .subscriptions import router as subscriptions_router
from .vapid import router as vapid_router

router = APIRouter(prefix="/notifications", tags=["Push Notifications"])

router.include_router(subscriptions_router)
router.include_router(vapid_router)
