"""
API endpoint для остановки зарядки
"""
from fastapi import APIRouter, Depends
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
    db: Session = Depends(get_db)
):
    """⏹️ Остановить зарядку с расчетом и возвратом средств"""
    
    service = ChargingService(db)
    
    try:
        result = await service.stop_charging_session(
            session_id=request.session_id,
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