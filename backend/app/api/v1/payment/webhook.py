from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
import xml.etree.ElementTree as ET

from app.db.session import get_db
from app.services.payment_provider_service import get_payment_provider_service
from app.schemas.ocpp import PaymentWebhookData

router = APIRouter()
logger = logging.getLogger(__name__)

async def process_balance_topup(topup_id: int, client_id: str, amount: float, invoice_id: str, provider_name: str):
    """Фоновая обработка пополнения баланса"""
    from app.db.session import get_session_local
    SessionLocal = get_session_local()
    db = SessionLocal()
    
    try:
        # Обновляем статус платежа
        db.execute(text("""
            UPDATE balance_topups 
            SET status = 'approved', paid_amount = :amount, paid_at = NOW(), 
                completed_at = NOW(), needs_status_check = false
            WHERE id = :topup_id
        """), {"topup_id": topup_id, "amount": amount})
        
        # Получаем текущий баланс клиента
        client_result = db.execute(text("""
            SELECT balance FROM clients WHERE id = :client_id
        """), {"client_id": client_id})
        
        client = client_result.fetchone()
        if not client:
            logger.error(f"Client {client_id} not found during balance topup")
            return
            
        old_balance = float(client[0])
        new_balance = old_balance + amount
        
        # Обновляем баланс клиента
        db.execute(text("""
            UPDATE clients SET balance = :new_balance WHERE id = :client_id
        """), {"new_balance": new_balance, "client_id": client_id})
        
        # Создаем запись о транзакции
        db.execute(text("""
            INSERT INTO payment_transactions_odengi 
            (client_id, transaction_type, amount, balance_before, balance_after, description, balance_topup_id)
            VALUES (:client_id, 'balance_topup', :amount, :balance_before, :balance_after, :description, :topup_id)
        """), {
            "client_id": client_id,
            "amount": amount,
            "balance_before": old_balance,
            "balance_after": new_balance,
            "description": f"Пополнение баланса через {provider_name}, invoice {invoice_id}",
            "topup_id": topup_id
        })
        
        db.commit()
        logger.info(f"✅ Пополнение выполнено: {client_id}, +{amount} сом, баланс: {old_balance} → {new_balance}")
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Ошибка обработки пополнения {invoice_id}: {e}")
    finally:
        db.close()

@router.post("/payment/webhook")
async def handle_payment_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """🔔 Обработка webhook уведомлений от платежных провайдеров - полная реализация"""
    try:
        # 1. Получение сырых данных + защита от реплея
        payload = await request.body()
        ts_header = request.headers.get('X-Timestamp', '')
        try:
            ts = int(ts_header) if ts_header else 0
        except Exception:
            ts = 0
        if ts and abs(int(time.time()) - ts) > 300:
            raise HTTPException(status_code=400, detail="stale_timestamp")
        
        # 2. Определяем провайдера и верифицируем подпись
        provider_name = get_payment_provider_service().get_provider_name()

        if provider_name == "OBANK":
            # ⚠️ OBANK временно отключен
            if not settings.OBANK_ENABLED:
                logger.warning(f"OBANK webhook received while OBANK disabled (from {request.client.host})")
                raise HTTPException(status_code=503, detail="OBANK temporarily disabled")

            # TODO: КРИТИЧНО - Реализовать проверку OBANK webhook при включении:
            # 1. IP whitelist для OBANK серверов
            # 2. SSL client certificate verification (mutual TLS)
            # 3. HMAC signature проверка (если OBANK предоставляет)
            logger.warning("OBANK webhook authentication not implemented - accepting without verification")
            is_valid = True
        else:  # O!Dengi
            webhook_signature = request.headers.get('X-O-Dengi-Signature', '')
            is_valid = get_payment_provider_service().verify_webhook(payload, webhook_signature)
        
        if not is_valid:
            logger.warning(f"Invalid webhook signature from {request.client.host} for provider {provider_name}")
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        # 3. Парсинг данных в зависимости от провайдера
        if provider_name == "OBANK":
            # Для OBANK парсим XML
            root = ET.fromstring(payload.decode('utf-8'))
            
            # Извлекаем данные из XML
            invoice_id = root.find('.//invoice_id').text if root.find('.//invoice_id') is not None else None
            status = root.find('.//status').text if root.find('.//status') is not None else None
            amount = root.find('.//sum').text if root.find('.//sum') is not None else None
            
            # Преобразуем статус OBANK в числовой формат
            status_mapping = {"completed": 1, "failed": 2, "cancelled": 2}
            numeric_status = status_mapping.get(status, 0)
            paid_amount = float(amount) / 1000 if amount and status == "completed" else None
            
        else:  # O!Dengi
            webhook_data = PaymentWebhookData.parse_raw(payload)
            invoice_id = webhook_data.invoice_id
            numeric_status = webhook_data.status
            paid_amount = webhook_data.paid_amount / 100 if webhook_data.paid_amount else None
        
        # 4. Поиск платежа в базе
        topup_check = db.execute(text("""
            SELECT id, client_id, requested_amount, status, payment_provider FROM balance_topups 
            WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        topup = topup_check.fetchone()
        
        if not topup:
            logger.warning(f"Payment not found for webhook: {invoice_id}")
            return {"status": "payment_not_found"}
        
        # 5. Проверяем что провайдер соответствует записи в БД
        if topup[4] != provider_name:
            logger.warning(f"Provider mismatch for payment {invoice_id}: expected {topup[4]}, got {provider_name}")
            return {"status": "provider_mismatch"}
        
        # 6. Обработка пополнения баланса
        if numeric_status == 1 and topup[3] != "approved":  # Оплачено
            background_tasks.add_task(
                process_balance_topup,
                topup[0],  # topup_id
                topup[1],  # client_id
                paid_amount if paid_amount else topup[2],  # amount
                invoice_id,
                provider_name
            )
        
        return {"status": "received", "invoice_id": invoice_id, "provider": provider_name}
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Webhook processing failed")