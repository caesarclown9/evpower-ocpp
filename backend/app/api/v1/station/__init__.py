from fastapi import APIRouter
from . import status

router = APIRouter()
router.include_router(status.router)