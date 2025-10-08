from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse, parse_qs, unquote

from app.db.session import get_db
from app.schemas.ocpp import BalanceTopupRequest, BalanceTopupResponse, H2HPaymentRequest, H2HPaymentResponse
from app.services.payment_provider_service import get_qr_payment_service, get_card_payment_service
from app.crud.ocpp_service import payment_lifecycle_service
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/balance/topup-qr", response_model=BalanceTopupResponse)
async def create_qr_balance_topup(
    request: BalanceTopupRequest,
    db: Session = Depends(get_db),
    http_request: Request = None
) -> BalanceTopupResponse:
    """🔥 Пополнение баланса через QR код (O!Dengi) - полная реализация"""
    client_id = getattr(http_request.state, "client_id", None)
    if not client_id:
        return BalanceTopupResponse(success=False, error="unauthorized", client_id="")

    logger.info(f"🔥 QR Topup request: client_id={client_id}, amount={request.amount}")
    
    try:
        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), {"client_id": client_id})
        client = client_check.fetchone()
        if not client:
            return BalanceTopupResponse(
                success=False,
                error="client_not_found",
                client_id=client_id
            )

        # 2. Отменяем существующие активные QR коды
        existing_pending = db.execute(text("""
            SELECT invoice_id FROM balance_topups 
            WHERE client_id = :client_id AND status = 'processing' 
            AND invoice_expires_at > NOW()
        """), {"client_id": request.client_id}).fetchall()
        
        if existing_pending:
            cancelled_invoices = [row.invoice_id for row in existing_pending]
            db.execute(text("""
                UPDATE balance_topups 
                SET status = 'canceled'
                WHERE client_id = :client_id AND status = 'processing'
                AND invoice_expires_at > NOW()
            """), {"client_id": request.client_id})
            
            logger.info(f"🔄 Отменены активные QR коды для клиента {request.client_id}: {cancelled_invoices}")
            db.commit()

        # 3. Генерация безопасного order_id
        order_id = f"qr_topup_{client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 4. Описание платежа
        description = request.description or f"Пополнение баланса через QR код: {request.amount} сом"
        
        # 5. Создание платежа через O!Dengi
        qr_payment_provider = get_qr_payment_service()
        notify_url = f"{settings.API_V1_STR}/payment/webhook"
        redirect_url = f"{settings.API_V1_STR}/payment/success"
        
        payment_response = await qr_payment_provider.create_payment(
            amount=Decimal(str(request.amount)),
            order_id=order_id,
            email=client_id + "@evpower.local",
            notify_url=notify_url,
            redirect_url=redirect_url,
            description=description,
            client_id=client_id
        )
        
        if not payment_response.get("success"):
            return BalanceTopupResponse(
                success=False,
                error="payment_provider_error",
                client_id=client_id
            )

        # 6. Получаем QR код из ODENGI ответа
        raw_response = payment_response.get("raw_response", {})
        qr_data = raw_response.get("data", {})
        
        qr_code_data = qr_data.get("qr")
        qr_code_url = qr_data.get("qr") or f"https://api.dengi.o.kg/qr.php?type=emvQr&data={qr_code_data}" if qr_code_data else None
        app_link_url = qr_data.get("link_app") or qr_data.get("app_link")
        
        logger.info(f"📱 ODENGI ответ: qr_data={qr_code_data[:50] if qr_code_data else None}...")
        logger.info(f"📱 ODENGI qr_url={qr_code_url}")
        logger.info(f"📱 ODENGI app_link={app_link_url}")
        
        # Если нет прямых данных QR, пытаемся извлечь из URL
        if not qr_code_data and qr_code_url:
            try:
                parsed_url = urlparse(qr_code_url)
                query_params = parse_qs(parsed_url.query)
                
                if 'data' in query_params and query_params['data']:
                    qr_code_data = unquote(query_params['data'][0])
                    logger.info(f"📱 Извлечены данные QR из URL: {qr_code_data[:50]}...")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось извлечь данные QR-кода из URL: {e}")
                qr_code_data = None
        
        # 7. Рассчитываем время жизни платежа
        created_at = datetime.now(timezone.utc)
        qr_expires_at, invoice_expires_at = payment_lifecycle_service.calculate_expiry_times(created_at)

        # 8. Сохранение в базу данных
        topup_insert = db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, qr_code_url, app_link, status, odengi_status,
             qr_expires_at, invoice_expires_at, needs_status_check, payment_provider)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, :qr_code_url, :app_link, 'processing', 0,
                    :qr_expires_at, :invoice_expires_at, true, :payment_provider)
            RETURNING id
        """), {
            "invoice_id": payment_response.get("invoice_id", payment_response.get("auth_key")),
            "order_id": order_id,
            "merchant_id": "ODENGI",
            "client_id": client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_code_url": qr_code_url,
            "app_link": app_link_url,
            "qr_expires_at": qr_expires_at,
            "invoice_expires_at": invoice_expires_at,
            "payment_provider": "ODENGI"
        })
        
        db.commit()
        
        invoice_id = payment_response.get("invoice_id", payment_response.get("auth_key"))
        logger.info(f"🔥 QR пополнение создано: {order_id}, invoice_id: {invoice_id}")
        
        # 9. Запускаем мониторинг статуса платежа
        async def check_payment_status_task():
            for i in range(20):
                await asyncio.sleep(15)
                try:
                    result = await payment_lifecycle_service.perform_status_check(
                        db, "balance_topups", invoice_id
                    )
                    if result.get("success"):
                        new_status = result.get("new_status")
                        if new_status in ['approved', 'canceled', 'refunded']:
                            return
                except Exception as e:
                    logger.error(f"Ошибка проверки QR платежа {invoice_id}: {e}")
                    
        asyncio.create_task(check_payment_status_task())
        logger.info(f"🔍 Запущен мониторинг QR платежа {invoice_id}")
        
        return BalanceTopupResponse(
            success=True,
            invoice_id=invoice_id,
            order_id=order_id,
            qr_code=qr_code_data,
            qr_code_url=qr_code_url,
            app_link=app_link_url,
            amount=request.amount,
            client_id=client_id,
            current_balance=float(client[1]),
            qr_expires_at=qr_expires_at,
            invoice_expires_at=invoice_expires_at,
            qr_lifetime_seconds=300,
            invoice_lifetime_seconds=600
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ QR Topup exception: {e}")
        return BalanceTopupResponse(
            success=False,
            error="internal_error",
            client_id=client_id or ""
        )

@router.post("/balance/topup-card", response_model=H2HPaymentResponse)
async def create_card_balance_topup(
    request: H2HPaymentRequest,
    db: Session = Depends(get_db),
    http_request: Request = None
) -> H2HPaymentResponse:
    """💳 Пополнение баланса банковской картой (OBANK) - полная реализация"""
    client_id = getattr(http_request.state, "client_id", None)
    if not client_id:
        return H2HPaymentResponse(success=False, error="unauthorized", client_id="")

    logger.info(f"Card Topup request received for client: {client_id}")
    
    try:
        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), {"client_id": client_id})
        client = client_check.fetchone()
        if not client:
            return H2HPaymentResponse(
                success=False,
                error="client_not_found",
                client_id=client_id
            )

        # 2. Принудительно используем OBANK для карт
        card_payment_provider = get_card_payment_service()
        
        # 3. Генерация безопасного order_id
        order_id = f"card_topup_{client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 4. Описание платежа
        description = request.description or f"Пополнение баланса картой: {request.amount} сом"
        
        # 5. Создание H2H платежа через OBANK
        h2h_response = await card_payment_provider.create_h2h_payment(
            amount=Decimal(str(request.amount)),
            order_id=order_id,
            card_data={
                "pan": request.card_pan,
                "name": request.card_name,
                "cvv": request.card_cvv,
                "year": request.card_year,
                "month": request.card_month
            },
            email=request.email,
            phone_number=request.phone_number,
            description=description
        )
        
        if not h2h_response.get("success"):
            logger.error(f"❌ Card payment failed: {h2h_response.get('error')}")
            return H2HPaymentResponse(
                success=False,
                error=h2h_response.get("error", "payment_provider_error"),
                client_id=request.client_id
            )
        
        # 6. Сохраняем платеж в balance_topups с данными OBANK
        auth_key = h2h_response.get("auth_key")
        transaction_id = h2h_response.get("transaction_id")
        
        created_at = datetime.now(timezone.utc)
        qr_expires_at, invoice_expires_at = payment_lifecycle_service.calculate_expiry_times(created_at)
        
        topup_insert = db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, status, odengi_status,
             qr_expires_at, invoice_expires_at, needs_status_check, payment_provider)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, 'processing', 0,
                    :qr_expires_at, :invoice_expires_at, true, :payment_provider)
            RETURNING id
        """), {
            "invoice_id": auth_key,  # Для OBANK используем auth_key как invoice_id
            "order_id": order_id,
            "merchant_id": "OBANK",
            "client_id": client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_expires_at": qr_expires_at,
            "invoice_expires_at": invoice_expires_at,
            "payment_provider": "OBANK"
        })
        
        db.commit()
        logger.info(f"💳 Card topup created: {order_id}, auth_key: {auth_key}")
        
        # 7. Запускаем мониторинг статуса платежа
        async def check_payment_status_task():
            for i in range(20):
                await asyncio.sleep(15)
                try:
                    result = await payment_lifecycle_service.perform_status_check(
                        db, "balance_topups", auth_key
                    )
                    if result.get("success"):
                        new_status = result.get("new_status")
                        if new_status in ['approved', 'canceled', 'refunded']:
                            return
                except Exception as e:
                    logger.error(f"Ошибка проверки card платежа {auth_key}: {e}")
                    
        asyncio.create_task(check_payment_status_task())
        
        return H2HPaymentResponse(
            success=True,
            auth_key=auth_key,
            transaction_id=transaction_id,
            order_id=order_id,
            amount=request.amount,
            client_id=client_id,
            current_balance=float(client[1]),
            redirect_url=h2h_response.get("redirect_url"),
            payment_url=h2h_response.get("payment_url")
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Card topup exception: {e}")
        return H2HPaymentResponse(
            success=False,
            error="internal_error",
            client_id=client_id or ""
        )