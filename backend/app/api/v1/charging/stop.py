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
from app.services.push_service import push_service, get_station_owner_id

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

        # Если зарядка успешно остановлена - отправляем push notifications
        if result.get("success"):
            session_id = result.get("session_id")
            station_id_val = result.get("station_id")
            energy_kwh = result.get("energy_consumed", 0)
            actual_cost = result.get("actual_cost", 0)

            # Push notification клиенту (graceful degradation)
            try:
                await push_service.send_to_client(
                    db=db,
                    client_id=client_id,
                    event_type="charging_completed",
                    session_id=session_id,
                    energy_kwh=energy_kwh,
                    amount=actual_cost
                )
                logger.info(f"Push notification sent to client {client_id} (charging completed)")
            except Exception as e:
                logger.warning(f"Failed to send push notification to client: {e}")

            # Push notification владельцу станции (graceful degradation)
            try:
                owner_id = get_station_owner_id(db, station_id_val)
                if owner_id:
                    await push_service.send_to_owner(
                        db=db,
                        owner_id=owner_id,
                        event_type="session_completed",
                        session_id=session_id,
                        station_id=station_id_val,
                        station_name=station_id_val,  # TODO: получить имя станции из БД
                        energy_kwh=energy_kwh,
                        amount=actual_cost
                    )
                    logger.info(f"Push notification sent to owner {owner_id} (session completed)")
            except Exception as e:
                logger.warning(f"Failed to send push notification to owner: {e}")

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