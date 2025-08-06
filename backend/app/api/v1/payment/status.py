from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
import asyncio

from app.db.session import get_db
from app.schemas.ocpp import PaymentStatusResponse
from app.crud.ocpp_service import payment_lifecycle_service

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/payment/status/{invoice_id}", response_model=PaymentStatusResponse)
async def get_payment_status(
    invoice_id: str,
    db: Session = Depends(get_db)
) -> PaymentStatusResponse:
    """📊 Проверка статуса платежа с учетом времени жизни - полная реализация"""
    try:
        # 1. Ищем платеж в таблице пополнений баланса
        topup_check = db.execute(text("""
            SELECT id, invoice_id, order_id, client_id, requested_amount, status, odengi_status,
                   qr_expires_at, invoice_expires_at, last_status_check_at, created_at
            FROM balance_topups WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        topup = topup_check.fetchone()
        
        if not topup:
            return PaymentStatusResponse(
                success=False,
                status=0,
                status_text="Платеж не найден",
                error="payment_not_found"
            )

        # 2. Проверяем время жизни
        qr_expires_at = topup[7]
        invoice_expires_at = topup[8]
        qr_expired = payment_lifecycle_service.is_qr_expired(qr_expires_at)
        invoice_expired = payment_lifecycle_service.is_invoice_expired(invoice_expires_at)
        
        # Если invoice истек - автоматически отменяем
        if invoice_expired and topup[5] == "processing":
            db.execute(text("""
                UPDATE balance_topups 
                SET status = 'canceled', completed_at = NOW(), needs_status_check = false
                WHERE invoice_id = :invoice_id
            """), {"invoice_id": invoice_id})
            db.commit()
            
            return PaymentStatusResponse(
                success=True,
                status=2,  # canceled
                status_text="Платеж отменен - время истекло",
                amount=float(topup[4]),
                invoice_id=invoice_id,
                qr_expired=True,
                invoice_expired=True,
                qr_expires_at=qr_expires_at,
                invoice_expires_at=invoice_expires_at
            )

        # 3. Читаем актуальный статус из базы данных
        fresh_topup_check = db.execute(text("""
            SELECT status, odengi_status, paid_amount, last_status_check_at
            FROM balance_topups WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        fresh_topup = fresh_topup_check.fetchone()
        if fresh_topup:
            db_status, db_odengi_status, db_paid_amount, db_last_check = fresh_topup
        else:
            db_status, db_odengi_status, db_paid_amount, db_last_check = topup[5], topup[6], None, topup[9]
        
        # 4. Маппинг статусов
        status_mapping = {
            "processing": 0,
            "approved": 1,
            "canceled": 2, 
            "refunded": 3,
            "partial_refund": 4
        }
        
        numeric_status = status_mapping.get(db_status, 0)
        
        # 5. Определение возможности операций
        can_proceed = (numeric_status == 1)  # Только для approved платежей
        needs_callback_check = (db_status == "processing" and 
                               not invoice_expired and 
                               payment_lifecycle_service.should_status_check(
                                   topup[10], db_last_check, 0, db_status))

        status_text_mapping = {
            0: "В процессе обработки",
            1: "Платеж зачислен",
            2: "Платеж отменен",
            3: "Платеж возвращен",
            4: "Частичный возврат"
        }
        
        return PaymentStatusResponse(
            success=True,
            status=numeric_status,
            status_text=status_text_mapping.get(numeric_status, "Неизвестный статус"),
            amount=float(db_paid_amount) if db_paid_amount else float(topup[4]),
            invoice_id=invoice_id,
            can_proceed=can_proceed,
            needs_callback_check=needs_callback_check,
            qr_expired=qr_expired,
            invoice_expired=invoice_expired,
            qr_expires_at=qr_expires_at,
            invoice_expires_at=invoice_expires_at,
            last_status_check_at=db_last_check
        )
        
    except Exception as e:
        logger.error(f"Ошибка получения статуса платежа {invoice_id}: {e}")
        return PaymentStatusResponse(
            success=False,
            status=0,
            status_text="Ошибка получения статуса",
            error="internal_error"
        )

@router.post("/payment/status-check/{invoice_id}")
async def force_payment_status_check(
    invoice_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """🔄 Принудительная проверка статуса платежа - полная реализация"""
    try:
        # Проверяем существование платежа
        topup_check = db.execute(text("""
            SELECT client_id, status FROM balance_topups 
            WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        topup = topup_check.fetchone()
        if not topup:
            return {"success": False, "error": "payment_not_found"}
        
        if topup[1] not in ["processing"]:
            return {"success": False, "error": "payment_not_processing"}
        
        # Запускаем фоновую проверку
        async def perform_status_check():
            try:
                result = await payment_lifecycle_service.perform_status_check(
                    db, "balance_topups", invoice_id
                )
                logger.info(f"🔄 Принудительная проверка {invoice_id}: {result}")
            except Exception as e:
                logger.error(f"❌ Ошибка принудительной проверки {invoice_id}: {e}")
        
        background_tasks.add_task(perform_status_check)
        
        return {
            "success": True,
            "message": "Проверка статуса запущена",
            "invoice_id": invoice_id
        }
        
    except Exception as e:
        logger.error(f"Ошибка запуска проверки статуса {invoice_id}: {e}")
        return {"success": False, "error": "internal_error"}

@router.get("/payment/h2h-status/{transaction_id}")
async def get_h2h_payment_status(
    transaction_id: str,
    db: Session = Depends(get_db)
):
    """📋 Статус H2H платежа - полная реализация"""
    try:
        # Ищем платеж по order_id (который содержит transaction_id)
        payment_check = db.execute(text("""
            SELECT invoice_id, status, client_id, requested_amount, paid_amount
            FROM balance_topups 
            WHERE order_id = :transaction_id OR invoice_id = :transaction_id
        """), {"transaction_id": transaction_id})
        
        payment = payment_check.fetchone()
        
        if not payment:
            return {
                "success": False,
                "error": "payment_not_found",
                "transaction_id": transaction_id
            }
        
        status_mapping = {
            "processing": "pending",
            "approved": "approved", 
            "canceled": "canceled",
            "refunded": "refunded"
        }
        
        return {
            "success": True,
            "transaction_id": transaction_id,
            "invoice_id": payment[0],
            "status": status_mapping.get(payment[1], "unknown"),
            "client_id": payment[2],
            "amount": float(payment[4]) if payment[4] else float(payment[3])
        }
        
    except Exception as e:
        logger.error(f"Ошибка получения статуса H2H платежа {transaction_id}: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "transaction_id": transaction_id
        }

@router.post("/payment/cancel/{invoice_id}")
async def cancel_payment(
    invoice_id: str,
    db: Session = Depends(get_db)
):
    """❌ Отмена платежа - полная реализация"""
    try:
        # Проверяем существование и статус платежа
        payment_check = db.execute(text("""
            SELECT status, client_id FROM balance_topups 
            WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        payment = payment_check.fetchone()
        
        if not payment:
            return {
                "success": False,
                "error": "payment_not_found",
                "invoice_id": invoice_id
            }
        
        if payment[0] != "processing":
            return {
                "success": False, 
                "error": "payment_not_cancellable",
                "status": payment[0],
                "invoice_id": invoice_id
            }
        
        # Отменяем платеж
        db.execute(text("""
            UPDATE balance_topups 
            SET status = 'canceled', completed_at = NOW(), needs_status_check = false
            WHERE invoice_id = :invoice_id AND status = 'processing'
        """), {"invoice_id": invoice_id})
        
        db.commit()
        
        logger.info(f"❌ Платеж {invoice_id} отменен вручную")
        
        return {
            "success": True,
            "message": "Платеж отменен",
            "invoice_id": invoice_id,
            "status": "canceled"
        }
        
    except Exception as e:
        logger.error(f"Ошибка отмены платежа {invoice_id}: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "invoice_id": invoice_id
        }