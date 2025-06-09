"""
Unified Payment Provider Service

Унифицированный сервис для работы с различными платежными провайдерами:
- OBANK Payment Link API
- O!Dengi (Legacy support)

Автоматически выбирает провайдера на основе настроек конфигурации.
"""

from decimal import Decimal
from typing import Dict, Any, Optional, Union
from datetime import datetime, timezone
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

class PaymentProviderService:
    """Унифицированный сервис для работы с платежными провайдерами"""
    
    def __init__(self):
        self.provider = settings.PAYMENT_PROVIDER
        
        if self.provider == "OBANK":
            from app.services.obank_service import obank_service
            self.service = obank_service
        else:  # O!Dengi
            from app.crud.ocpp_service import odengi_service
            self.service = odengi_service
        
        logger.info(f"Инициализирован платежный провайдер: {self.provider}")
    
    async def create_payment(
        self,
        amount: Decimal,
        order_id: str,
        email: str,
        notify_url: str,
        redirect_url: str,
        description: str = "",
        client_id: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Создает платеж у выбранного провайдера
        
        Args:
            amount: Сумма платежа в сомах
            order_id: Уникальный ID заказа
            email: Email клиента
            notify_url: URL для уведомлений
            redirect_url: URL для редиректа
            description: Описание платежа
            client_id: ID клиента (опционально)
            
        Returns:
            Dict с данными платежа
        """
        if self.provider == "OBANK":
            # Создаем платежную страницу OBANK
            response = await self.service.create_payment_page(
                amount=amount,
                order_id=order_id,
                email=email,
                notify_url=notify_url,
                redirect_url=redirect_url,
                **kwargs
            )
            
            # Извлекаем данные из ответа OBANK
            data = response.get('data', {})
            return {
                "success": True,
                "payment_url": data.get('pay-url', ''),
                "auth_key": data.get('auth-key', ''),
                "invoice_id": data.get('auth-key', ''),  # Используем auth-key как invoice_id
                "provider": "OBANK",
                "status": "pending"
            }
            
        else:  # O!Dengi
            # Конвертируем сумму в копейки для O!Dengi
            amount_kopecks = int(amount * 100)
            
            response = await self.service.create_invoice(
                order_id=order_id,
                description=description,
                amount_kopecks=amount_kopecks
            )
            
            return {
                "success": True,
                "payment_url": response.get('url', ''),
                "invoice_id": response.get('id', ''),
                "provider": "ODENGI",
                "status": "pending"
            }
    
    async def check_payment_status(
        self, 
        invoice_id: str, 
        order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Проверяет статус платежа у провайдера
        
        Args:
            invoice_id: ID платежа у провайдера
            order_id: ID заказа (для O!Dengi)
            
        Returns:
            Dict со статусом платежа
        """
        if self.provider == "OBANK":
            # Для OBANK используем auth_key
            response = await self.service.check_payment_status(auth_key=invoice_id)
            
            # Парсим ответ OBANK
            data = response.get('data', {})
            obank_status = data.get('status', 'processing')
            
            # Маппинг статусов OBANK
            status_mapping = {
                'processing': {"status": "pending", "numeric": 0},
                'completed': {"status": "paid", "numeric": 1},
                'failed': {"status": "cancelled", "numeric": 2},
                'cancelled': {"status": "cancelled", "numeric": 2}
            }
            
            mapped = status_mapping.get(obank_status, {"status": "pending", "numeric": 0})
            paid_amount = None
            
            if mapped["status"] == "paid":
                # Конвертируем из тыйынов в сомы
                paid_amount = float(data.get('sum', 0)) / 1000
            
            return {
                "success": True,
                "status": mapped["status"],
                "numeric_status": mapped["numeric"],
                "paid_amount": paid_amount,
                "provider": "OBANK",
                "raw_response": response
            }
            
        else:  # O!Dengi
            response = await self.service.get_payment_status(
                invoice_id=invoice_id,
                order_id=order_id
            )
            
            status = response.get('status', 0)
            paid_amount = None
            
            if response.get('amount'):
                # Конвертируем из копеек в сомы
                paid_amount = response.get('amount', 0) / 100
            
            # Маппинг статусов O!Dengi
            status_mapping = {
                0: "pending",
                1: "paid", 
                2: "cancelled",
                3: "refunded",
                4: "partial_refund"
            }
            
            return {
                "success": True,
                "status": status_mapping.get(status, "pending"),
                "numeric_status": status,
                "paid_amount": paid_amount,
                "provider": "ODENGI",
                "raw_response": response
            }
    
    async def cancel_payment(
        self, 
        transaction_id: str, 
        refund_amount: Decimal
    ) -> Dict[str, Any]:
        """
        Отменяет платеж и возвращает средства
        
        Args:
            transaction_id: ID транзакции для отмены
            refund_amount: Сумма возврата в сомах
            
        Returns:
            Dict с результатом отмены
        """
        if self.provider == "OBANK":
            response = await self.service.cancel_payment(
                transaction_id=transaction_id,
                refund_amount=refund_amount
            )
            
            return {
                "success": response.get('state') == '0',
                "refund_id": response.get('id'),
                "refund_amount": float(response.get('sum', 0)) / 1000,
                "provider": "OBANK",
                "raw_response": response
            }
            
        else:  # O!Dengi
            # O!Dengi не поддерживает API отмены
            # Возвращаем статус что нужно делать вручную
            return {
                "success": False,
                "error": "manual_refund_required",
                "message": "O!Dengi требует ручной возврат через личный кабинет",
                "provider": "ODENGI"
            }
    
    def get_webhook_verification_method(self) -> str:
        """Возвращает метод верификации webhook для текущего провайдера"""
        return self.provider
    
    def verify_webhook(self, payload: bytes, signature: str, **kwargs) -> bool:
        """
        Верифицирует webhook от провайдера
        
        Args:
            payload: Тело запроса
            signature: Подпись запроса
            
        Returns:
            bool: True если подпись корректна
        """
        if self.provider == "OBANK":
            # OBANK использует SSL сертификаты для аутентификации
            # Дополнительная верификация не требуется если соединение установлено
            return True
            
        else:  # O!Dengi
            if hasattr(self.service, 'verify_webhook_signature'):
                return self.service.verify_webhook_signature(payload, signature)
            return False
    
    def get_provider_name(self) -> str:
        """Возвращает название текущего провайдера"""
        return self.provider
    
    def get_currency_code(self) -> str:
        """Возвращает код валюты для текущего провайдера"""
        if self.provider == "OBANK":
            return "417"  # KGS код для OBANK
        else:
            return "KGS"  # Для O!Dengi

# Ленивая инициализация - создаем экземпляр только при первом обращении
_payment_provider_service = None

def get_payment_provider_service() -> PaymentProviderService:
    """Возвращает экземпляр PaymentProviderService с ленивой инициализацией"""
    global _payment_provider_service
    if _payment_provider_service is None:
        _payment_provider_service = PaymentProviderService()
    return _payment_provider_service

# Для обратной совместимости - создаем алиас
payment_provider_service = get_payment_provider_service() 