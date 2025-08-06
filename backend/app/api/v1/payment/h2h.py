from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.db.session import get_db
from app.schemas.ocpp import H2HPaymentRequest, H2HPaymentResponse
from app.services.obank_service import obank_service
from app.crud.ocpp_service import payment_lifecycle_service
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/payment/h2h-payment", response_model=H2HPaymentResponse)
async def create_h2h_payment(
    request: H2HPaymentRequest,
    db: Session = Depends(get_db)
) -> H2HPaymentResponse:
    """💳 Host2Host платеж картой (прямой ввод данных карты) - полная реализация"""
    try:
        # Проверяем что используется OBANK
        if settings.PAYMENT_PROVIDER != "OBANK":
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="h2h_not_supported",
                message="H2H платежи поддерживаются только через OBANK"
            )

        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        client = client_check.fetchone()
        if not client:
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="client_not_found"
            )

        # 2. Проверяем существующие processing платежи
        existing_pending = db.execute(text("""
            SELECT invoice_id FROM balance_topups 
            WHERE client_id = :client_id AND status = 'processing' 
            AND invoice_expires_at > NOW()
        """), {"client_id": request.client_id}).fetchone()
        
        if existing_pending:
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="pending_payment_exists"
            )

        # 3. Генерируем уникальный transaction ID
        transaction_id = f"h2h_{request.client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 4. Описание платежа
        description = request.description or f"H2H пополнение баланса клиента {request.client_id} на {request.amount} сом"
        
        # 5. Создаем H2H платеж через OBANK
        notify_url = f"{settings.API_V1_STR}/payment/webhook"
        redirect_url = f"{settings.API_V1_STR}/payment/success"
        
        h2h_response = await obank_service.create_h2h_payment(
            amount=Decimal(str(request.amount)),
            transaction_id=transaction_id,
            account=request.card_pan[-4:],  # Последние 4 цифры карты как account
            email=request.email,
            notify_url=notify_url,
            redirect_url=redirect_url,
            card_pan=request.card_pan,
            card_name=request.card_name,
            card_cvv=request.card_cvv,
            card_year=request.card_year,
            card_month=request.card_month,
            phone_number=request.phone_number
        )
        
        if h2h_response.get("code") != "0":
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="h2h_payment_failed",
                message=h2h_response.get("message", "Ошибка H2H платежа")
            )

        # 6. Сохраняем в базу данных
        auth_key = h2h_response.get("data", {}).get("auth-key", transaction_id)
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
        logger.info(f"H2H payment created: auth_key={auth_key}, transaction_id={transaction_id}")

        return H2HPaymentResponse(
            success=True,
            auth_key=auth_key,
            transaction_id=transaction_id,
            order_id=transaction_id,
            amount=request.amount,
            client_id=request.client_id,
            current_balance=float(client[1]),
            redirect_url=h2h_response.get("data", {}).get("redirect-url"),
            payment_url=h2h_response.get("data", {}).get("payment-url")
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ H2H payment exception: {e}")
        return H2HPaymentResponse(
            success=False,
            client_id=request.client_id,
            error="internal_error"
        )