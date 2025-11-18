"""
API endpoint для начала зарядки
"""
from fastapi import APIRouter, Depends, HTTPException, Request
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
from app.services.push_service import push_service, get_station_owner_id

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/charging/start")
async def start_charging(
    request: ChargingStartRequest,
    db: Session = Depends(get_db),
    http_request: Request = None
):
    """🔌 Начать зарядку с проверкой баланса и снятием средств"""
    
    # Аутентификация: client_id из JWT/HMAC middleware
    client_id = getattr(http_request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    # Логируем запрос
    logger.info(f"Starting charging: client_id={client_id}, station_id={request.station_id}")
    
    service = ChargingService(db)
    
    try:
        # Делегируем бизнес-логику в сервис
        result = await service.start_charging_session(
            client_id=client_id,
            station_id=request.station_id,
            connector_id=request.connector_id,
            energy_kwh=request.energy_kwh,
            amount_som=request.amount_som,
            redis_manager=redis_manager
        )

        # Если зарядка успешно начата - отправляем push notifications
        if result.get("success"):
            session_id = result.get("session_id")
            station_id_val = result.get("station_id")
            connector_id_val = result.get("connector_id")

            # Push notification клиенту (graceful degradation)
            try:
                await push_service.send_to_client(
                    db=db,
                    client_id=client_id,
                    event_type="charging_started",
                    session_id=session_id,
                    station_id=station_id_val,
                    connector_id=connector_id_val
                )
                logger.info(f"Push notification sent to client {client_id} (charging started)")
            except Exception as e:
                logger.warning(f"Failed to send push notification to client: {e}")

            # Push notification владельцу станции (graceful degradation)
            try:
                owner_id = get_station_owner_id(db, station_id_val)
                if owner_id:
                    await push_service.send_to_owner(
                        db=db,
                        owner_id=owner_id,
                        event_type="new_session",
                        session_id=session_id,
                        station_id=station_id_val,
                        station_name=station_id_val,  # TODO: получить имя станции из БД
                        connector_id=connector_id_val
                    )
                    logger.info(f"Push notification sent to owner {owner_id} (new session)")
            except Exception as e:
                logger.warning(f"Failed to send push notification to owner: {e}")

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