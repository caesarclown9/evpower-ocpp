"""
API endpoint для начала зарядки
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timezone
from decimal import Decimal
import logging

from app.db.session import get_db
from ocpp_ws_server.redis_manager import redis_manager
# Аутентификация через FlutterFlow - client_id передается в запросе
from app.crud.ocpp_service import payment_service
from .schemas import ChargingStartRequest
from .service import ChargingService

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/charging/start")
async def start_charging(
    request: ChargingStartRequest, 
    db: Session = Depends(get_db)
):
    """🔌 Начать зарядку с проверкой баланса и снятием средств"""
    
    # Логируем запрос
    logger.info(f"Starting charging: client_id={request.client_id}, station_id={request.station_id}")
    
    service = ChargingService(db)
    
    try:
        # Делегируем бизнес-логику в сервис
        result = await service.start_charging_session(
            client_id=request.client_id,
            station_id=request.station_id,
            connector_id=request.connector_id,
            energy_kwh=request.energy_kwh,
            amount_som=request.amount_som,
            redis_manager=redis_manager
        )
        
        return result
        
    except ValueError as e:
        db.rollback()
        logger.error(f"Ошибка баланса при запуске зарядки: {e}")
        return {
            "success": False,
            "error": "balance_error",
            "message": str(e)
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при запуске зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "message": "Внутренняя ошибка сервера"
        }