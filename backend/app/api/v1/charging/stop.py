"""
API endpoint для остановки зарядки
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
import logging

from app.db.session import get_db
from ocpp_ws_server.redis_manager import redis_manager
# Аутентификация через FlutterFlow - client_id в сессии
from .schemas import ChargingStopRequest
from .service import ChargingService

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/charging/stop")
async def stop_charging(
    request: ChargingStopRequest,
    db: Session = Depends(get_db),
    http_request: Request = None
):
    """⏹️ Остановить зарядку с расчетом и возвратом средств"""

    # Аутентификация: client_id из JWT/HMAC middleware
    client_id = getattr(http_request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    # Логируем запрос
    logger.info(f"Stopping charging: client_id={client_id}, session_id={request.session_id}")

    service = ChargingService(db)

    try:
        result = await service.stop_charging_session(
            session_id=request.session_id,
            client_id=client_id,
            redis_manager=redis_manager
        )
        
        return result
        
    except ValueError as e:
        db.rollback()
        logger.error(f"Ошибка баланса при остановке зарядки: {e}")
        return {
            "success": False,
            "error": "balance_error",
            "message": "Ошибка получения баланса"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка остановки зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error", 
            "message": "Внутренняя ошибка сервера"
        }