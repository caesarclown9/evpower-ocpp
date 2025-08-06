"""
Middleware для аудита платежных операций
"""
import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional
from fastapi import Request, Response
from sqlalchemy import text
from app.db.session import get_db
from .logging_config import correlation_id
from .secure_logging import sanitize_dict

logger = logging.getLogger(__name__)

class PaymentAuditMiddleware:
    """Middleware для логирования и аудита платежных операций"""
    
    # Endpoints требующие аудита
    PAYMENT_ENDPOINTS = {
        "/api/balance/topup-qr",
        "/api/balance/topup-card", 
        "/api/balance/h2h-payment",
        "/api/balance/token-payment",
        "/api/payment/create-token",
        "/api/payment/webhook",
        "/api/payment/cancel",
        "/api/charging/start",  # Списание средств
        "/api/charging/stop"    # Возврат средств
    }
    
    async def __call__(self, request: Request, call_next):
        """Обрабатывает запросы с аудитом платежных операций"""
        
        # Проверяем, нужен ли аудит для этого endpoint
        path = str(request.url.path)
        needs_audit = any(path.startswith(endpoint) for endpoint in self.PAYMENT_ENDPOINTS)
        
        if not needs_audit:
            return await call_next(request)
        
        # Начало обработки
        start_time = datetime.utcnow()
        request_id = correlation_id.get()
        
        # Сохраняем тело запроса для аудита
        request_body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
                request_body = json.loads(body) if body else {}
                # Важно: восстанавливаем тело для дальнейшей обработки
                async def receive():
                    return {"type": "http.request", "body": body}
                request._receive = receive
            except:
                pass
        
        # Логируем начало операции
        audit_entry = {
            "event": "payment_operation_start",
            "request_id": request_id,
            "timestamp": start_time.isoformat(),
            "method": request.method,
            "path": path,
            "client_ip": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", ""),
            "auth_header_present": "authorization" in request.headers
        }
        
        # Добавляем безопасную версию тела запроса
        if request_body:
            audit_entry["request_body"] = sanitize_dict(request_body)
        
        logger.info(f"Payment operation started: {path}", extra=audit_entry)
        
        # Обрабатываем запрос
        response = None
        error = None
        
        try:
            response = await call_next(request)
            return response
            
        except Exception as e:
            error = str(e)
            raise
            
        finally:
            # Логируем результат операции
            end_time = datetime.utcnow()
            duration_ms = int((end_time - start_time).total_seconds() * 1000)
            
            audit_result = {
                "event": "payment_operation_complete",
                "request_id": request_id,
                "timestamp": end_time.isoformat(),
                "duration_ms": duration_ms,
                "status_code": response.status_code if response else 500,
                "success": response and 200 <= response.status_code < 400,
                "error": error
            }
            
            # Сохраняем в базу данных для критических операций
            if path in ["/api/balance/topup-card", "/api/balance/h2h-payment", "/api/charging/start"]:
                await self._save_audit_to_db(
                    request_id=request_id,
                    operation_type=self._get_operation_type(path),
                    request_data=audit_entry,
                    response_data=audit_result,
                    client_ip=audit_entry["client_ip"]
                )
            
            logger.info(f"Payment operation completed: {path}", extra=audit_result)
    
    def _get_operation_type(self, path: str) -> str:
        """Определяет тип операции по пути"""
        if "topup" in path:
            return "balance_topup"
        elif "h2h-payment" in path:
            return "card_payment"
        elif "token-payment" in path:
            return "token_payment"
        elif "charging/start" in path:
            return "charging_start"
        elif "charging/stop" in path:
            return "charging_stop"
        elif "webhook" in path:
            return "payment_webhook"
        else:
            return "payment_operation"
    
    async def _save_audit_to_db(
        self,
        request_id: str,
        operation_type: str,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        client_ip: str
    ):
        """Сохраняет аудит в базу данных"""
        try:
            db = next(get_db())
            
            # Извлекаем важные данные
            client_id = None
            amount = None
            
            if request_data.get("request_body"):
                body = request_data["request_body"]
                client_id = body.get("client_id")
                amount = body.get("amount")
            
            # Сохраняем аудит
            db.execute(text("""
                INSERT INTO payment_audit_log (
                    request_id, operation_type, client_id, amount,
                    client_ip, request_data, response_data, 
                    success, created_at
                ) VALUES (
                    :request_id, :operation_type, :client_id, :amount,
                    :client_ip, :request_data::jsonb, :response_data::jsonb,
                    :success, NOW()
                )
            """), {
                "request_id": request_id,
                "operation_type": operation_type,
                "client_id": client_id,
                "amount": amount,
                "client_ip": client_ip,
                "request_data": json.dumps(request_data),
                "response_data": json.dumps(response_data),
                "success": response_data.get("success", False)
            })
            
            db.commit()
            
        except Exception as e:
            logger.error(f"Failed to save payment audit: {e}")
        finally:
            if 'db' in locals():
                db.close()

# Функция для создания таблицы аудита (запустить при миграции)
def create_audit_table_sql():
    """SQL для создания таблицы аудита платежей"""
    return """
    CREATE TABLE IF NOT EXISTS payment_audit_log (
        id SERIAL PRIMARY KEY,
        request_id VARCHAR(50) NOT NULL,
        operation_type VARCHAR(50) NOT NULL,
        client_id VARCHAR(20),
        amount NUMERIC(10,2),
        client_ip VARCHAR(45),
        request_data JSONB,
        response_data JSONB,
        success BOOLEAN DEFAULT false,
        created_at TIMESTAMP DEFAULT NOW(),
        
        -- Индексы для быстрого поиска
        INDEX idx_payment_audit_request_id (request_id),
        INDEX idx_payment_audit_client_id (client_id),
        INDEX idx_payment_audit_created_at (created_at),
        INDEX idx_payment_audit_operation_type (operation_type)
    );
    
    -- Партиционирование по месяцам для больших объемов
    -- CREATE TABLE payment_audit_log_2024_01 PARTITION OF payment_audit_log
    -- FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
    """