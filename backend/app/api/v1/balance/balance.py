from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging

from app.db.session import get_db
from app.schemas.ocpp import ClientBalanceInfo
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/balance/{client_id}", response_model=ClientBalanceInfo)
async def get_client_balance(
    client_id: str, 
    db: Session = Depends(get_db)
) -> ClientBalanceInfo:
    """💰 Получение информации о балансе клиента"""
    try:
        # Получаем информацию о клиенте и балансе
        client_info = db.execute(text("""
            SELECT id, balance, updated_at FROM clients WHERE id = :client_id
        """), {"client_id": client_id})
        
        client = client_info.fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Клиент не найден")
        
        # Получаем дату последнего пополнения
        last_topup = db.execute(text("""
            SELECT paid_at FROM balance_topups 
            WHERE client_id = :client_id AND status = 'approved'
            ORDER BY paid_at DESC LIMIT 1
        """), {"client_id": client_id})
        
        last_topup_date = last_topup.fetchone()
        
        # Подсчитываем общую потраченную сумму (резерв минус возвраты плюс доплаты)
        total_spent = db.execute(text("""
            SELECT COALESCE(SUM(CASE 
                WHEN transaction_type = 'charge_reserve' THEN ABS(amount)
                WHEN transaction_type = 'charge_refund' THEN -ABS(amount) 
                WHEN transaction_type = 'charge_payment' THEN ABS(amount)
                ELSE 0 END), 0) 
            FROM payment_transactions_odengi 
            WHERE client_id = :client_id AND transaction_type IN ('charge_reserve', 'charge_refund', 'charge_payment')
        """), {"client_id": client_id})
        
        spent_amount = total_spent.fetchone()[0]
        
        return ClientBalanceInfo(
            client_id=client_id,
            balance=float(client[1]),
            currency=settings.DEFAULT_CURRENCY,
            last_topup_at=last_topup_date[0] if last_topup_date else None,
            total_spent=float(spent_amount) if spent_amount else 0
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка получения баланса клиента {client_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка получения баланса")


@router.get("/balance/get", response_model=ClientBalanceInfo)
async def get_balance_authenticated(
    request: Request,
    db: Session = Depends(get_db)
) -> ClientBalanceInfo:
    """
    💰 Получение баланса текущего аутентифицированного пользователя

    Endpoint для фронтенда, автоматически определяет client_id из токена аутентификации.
    Не требует передачи client_id в URL.

    Returns:
        ClientBalanceInfo: Информация о балансе клиента

    Raises:
        HTTPException: 401 если пользователь не аутентифицирован
        HTTPException: 404 если клиент не найден
        HTTPException: 500 при ошибке получения баланса
    """
    # Получаем client_id из request.state (устанавливается AuthMiddleware)
    client_id = getattr(request.state, "client_id", None)

    if not client_id:
        logger.warning("Попытка получить баланс без аутентификации")
        raise HTTPException(
            status_code=401,
            detail="Требуется аутентификация"
        )

    # Переиспользуем существующую логику
    return await get_client_balance(client_id, db)