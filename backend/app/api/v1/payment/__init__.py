from fastapi import APIRouter
from . import status, webhook, h2h, token

router = APIRouter()
router.include_router(status.router)
router.include_router(webhook.router)
router.include_router(h2h.router)
router.include_router(token.router)