"""
📊 Endpoint для получения статуса зарядки
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import logging

from app.db.session import get_db
from app.core.auth import optional_authentication

from .schemas import ChargingStatusResponse
from .service import ChargingService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/charging/status/{session_id}", response_model=ChargingStatusResponse)
async def get_charging_status(
    session_id: str, 
    db: Session = Depends(get_db),
    current_user: dict = Depends(optional_authentication)
):
    """📊 Проверить статус зарядки с полными данными из OCPP"""
    
    try:
        # Создаем сервис и получаем статус
        charging_service = ChargingService(db)
        result = await charging_service.get_charging_status(session_id)
        
        # Возвращаем результат
        if result["success"]:
            logger.debug(f"📊 Статус зарядки получен: сессия {session_id}, статус {result.get('status', 'unknown')}")
            return ChargingStatusResponse(**result)
        else:
            logger.warning(f"❌ Ошибка получения статуса зарядки: {result.get('error', 'unknown')}")
            raise HTTPException(
                status_code=404 if result.get("error") == "session_not_found" else 500,
                detail={
                    "error": result.get("error", "internal_error"),
                    "message": result.get("message", "Внутренняя ошибка сервера"),
                    "success": False
                }
            )
    
    except HTTPException:
        # Пропускаем HTTP исключения как есть
        raise
    
    except Exception as e:
        logger.error(f"💥 Критическая ошибка в endpoint статуса зарядки: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Внутренняя ошибка сервера",
                "success": False
            }
        )