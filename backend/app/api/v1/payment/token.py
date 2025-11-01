from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.db.session import get_db
from app.schemas.ocpp import CreateTokenResponse, TokenPaymentRequest, TokenPaymentResponse
from app.services.obank_service import obank_service
from app.crud.ocpp_service import payment_lifecycle_service
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/payment/create-token", response_model=CreateTokenResponse)
async def create_payment_token(
    db: Session = Depends(get_db)
):
    """
    🔐 Создание токена для платежей

    ⚠️ ВРЕМЕННО ОТКЛЮЧЕНО: OBANK интеграция в разработке
    """
    try:
        # Проверяем что OBANK включен
        if not settings.OBANK_ENABLED or settings.PAYMENT_PROVIDER != "OBANK":
            logger.warning(f"Token creation attempt while OBANK disabled")
            return CreateTokenResponse(
                success=False,
                error="token_not_available",
                message="Token платежи временно недоступны"
            )
        
        # Создание токена через OBANK
        token_response = await obank_service.create_payment_token()
        
        if token_response.get("code") != "0":
            return CreateTokenResponse(
                success=False,
                error="token_creation_failed",
                message=token_response.get("message", "Ошибка создания токена")
            )
        
        token_data = token_response.get("data", {})
        
        return CreateTokenResponse(
            success=True,
            token=token_data.get("token"),
            expires_at=token_data.get("expires_at"),
            payment_url=token_data.get("payment_url")
        )
        
    except Exception as e:
        logger.error(f"❌ Token creation exception: {e}")
        return CreateTokenResponse(
            success=False,
            error="internal_error"
        )

@router.post("/payment/token-payment", response_model=TokenPaymentResponse)
async def create_token_payment(
    request: TokenPaymentRequest,
    db: Session = Depends(get_db)
) -> TokenPaymentResponse:
    """
    🔐 Токен-платеж для пополнения баланса

    ⚠️ ВРЕМЕННО ОТКЛЮЧЕНО: OBANK интеграция в разработке
    """
    try:
        # Проверяем что OBANK включен
        if not settings.OBANK_ENABLED or settings.PAYMENT_PROVIDER != "OBANK":
            logger.warning(f"Token payment attempt while OBANK disabled")
            return TokenPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="token_not_available",
                message="Token платежи временно недоступны. Используйте QR-код пополнение."
            )

        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        client = client_check.fetchone()
        if not client:
            return TokenPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="client_not_found"
            )

        # 2. Генерируем уникальный transaction ID
        transaction_id = f"token_{request.client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 3. Описание платежа
        description = request.description or f"Token пополнение баланса клиента {request.client_id} на {request.amount} сом"
        
        # 4. Создаем токен-платеж через OBANK
        notify_url = f"{settings.API_V1_STR}/payment/webhook"
        redirect_url = f"{settings.API_V1_STR}/payment/success"
        
        token_response = await obank_service.create_token_payment(
            amount=Decimal(str(request.amount)),
            transaction_id=transaction_id,
            token=request.token,
            account=request.account or "default",
            email=request.email,
            notify_url=notify_url,
            redirect_url=redirect_url,
            description=description
        )
        
        if token_response.get("code") != "0":
            return TokenPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="token_payment_failed",
                message=token_response.get("message", "Ошибка token платежа")
            )

        # 5. Сохраняем в базу данных
        auth_key = token_response.get("data", {}).get("auth-key", transaction_id)
        created_at = datetime.now(timezone.utc)
        qr_expires_at, invoice_expires_at = payment_lifecycle_service.calculate_expiry_times(created_at)

        db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, status, odengi_status,
             qr_expires_at, invoice_expires_at, needs_status_check, payment_provider)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, 'processing', 0,
                    :qr_expires_at, :invoice_expires_at, true, :payment_provider)
        """), {
            "invoice_id": auth_key,
            "order_id": transaction_id,
            "merchant_id": "OBANK",
            "client_id": request.client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_expires_at": qr_expires_at,
            "invoice_expires_at": invoice_expires_at,
            "payment_provider": "OBANK"
        })

        db.commit()
        logger.info(f"Token payment created: auth_key={auth_key}, transaction_id={transaction_id}")

        return TokenPaymentResponse(
            success=True,
            auth_key=auth_key,
            transaction_id=transaction_id,
            order_id=transaction_id,
            amount=request.amount,
            client_id=request.client_id,
            current_balance=float(client[1]),
            redirect_url=token_response.get("data", {}).get("redirect-url"),
            payment_url=token_response.get("data", {}).get("payment-url")
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Token payment exception: {e}")
        return TokenPaymentResponse(
            success=False,
            client_id=request.client_id,
            error="internal_error"
        )