from fastapi import APIRouter
from . import balance, topup, payment

router = APIRouter()
router.include_router(balance.router)
router.include_router(topup.router) 
router.include_router(payment.router)