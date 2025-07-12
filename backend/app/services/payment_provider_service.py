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
    
    def __init__(self, force_provider: Optional[str] = None):
        """
        Инициализация провайдера
        
        Args:
            force_provider: Принудительно использовать конкретный провайдер ("OBANK" или "ODENGI")
                           Если None - использует настройку из config
        """
        self.provider = force_provider or settings.PAYMENT_PROVIDER
        
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
            
            # ODENGI возвращает invoice_id в поле data согласно документации
            data = response.get('data', {})
            invoice_id = (data.get('invoice_id') or 
                         response.get('invoice_id') or
                         response.get('id') or
                         order_id)  # fallback к order_id если API не вернул ID
            
            logger.info(f"📱 ODENGI extracted invoice_id: {invoice_id}")
            
            payment_url = (response.get('url') or 
                          response.get('pay_url') or
                          response.get('data', {}).get('url') or '')
            
            return {
                "success": True,
                "payment_url": payment_url,
                "invoice_id": invoice_id,
                "provider": "ODENGI",
                "status": "processing",
                "raw_response": response  # Для дебага
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
            
            # Маппинг статусов OBANK (обновлённые)
            status_mapping = {
                'processing': {"status": "processing", "numeric": 0},
                'completed': {"status": "approved", "numeric": 1},
                'failed': {"status": "canceled", "numeric": 2},
                'cancelled': {"status": "canceled", "numeric": 2}
            }
            
            mapped = status_mapping.get(obank_status, {"status": "processing", "numeric": 0})
            paid_amount = None
            
            if mapped["status"] == "approved":
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
            
            # ODENGI возвращает текстовый статус в data.status
            data = response.get('data', {})
            status_text = data.get('status', 'processing')
            paid_amount = None
            
            if data.get('amount'):
                # Конвертируем из копеек в сомы
                paid_amount = data.get('amount', 0) / 100
            
            # Маппинг ТЕКСТОВЫХ статусов ODENGI (как возвращает API)
            status_mapping = {
                'processing': "processing",
                'approved': "approved", 
                'canceled': "canceled"
            }
            
            # Числовой статус для обратной совместимости
            numeric_mapping = {
                'processing': 0,
                'approved': 1,
                'canceled': 2
            }
            
            return {
                "success": True,
                "status": status_mapping.get(status_text, "processing"),
                "numeric_status": numeric_mapping.get(status_text, 0),
                "paid_amount": paid_amount,
                "provider": "ODENGI",
                "raw_response": response
            }
    
    async def create_h2h_payment(
        self,
        amount: Decimal,
        order_id: str,
        card_data: Dict[str, str],
        email: str,
        phone_number: Optional[str] = None,
        description: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Создает H2H платеж картой (только OBANK)
        
        Args:
            amount: Сумма платежа в сомах
            order_id: Уникальный ID заказа
            card_data: Данные карты (pan, name, cvv, year, month)
            email: Email клиента
            phone_number: Телефон клиента
            description: Описание платежа
            
        Returns:
            Dict с результатом платежа
        """
        if self.provider != "OBANK":
            return {
                "success": False,
                "error": "h2h_not_supported",
                "message": f"H2H платежи поддерживаются только провайдером OBANK, текущий: {self.provider}"
            }
        
        try:
            response = await self.service.create_h2h_payment(
                amount_kgs=float(amount),
                client_id=order_id,
                card_data={
                    "number": card_data.get('pan', ''),
                    "holder_name": card_data.get('name', ''),
                    "cvv": card_data.get('cvv', ''),
                    "exp_year": card_data.get('year', ''),
                    "exp_month": card_data.get('month', ''),
                    "email": email,
                    "phone": phone_number or '+996700000000'
                }
            )
            
            if response.get('success'):
                return {
                    "success": True,
                    "transaction_id": response.get('transaction_id'),
                    "auth_key": response.get('auth_key'),
                    "status": response.get('status', 'processing'),
                    "message": response.get('message', 'H2H платеж создан'),
                    "provider": "OBANK"
                }
            else:
                return {
                    "success": False,
                    "error": response.get('error', 'h2h_creation_failed'),
                    "message": response.get('message', 'Ошибка создания H2H платежа')
                }
                
        except Exception as e:
            logger.error(f"H2H payment creation error: {e}")
            return {
                "success": False,
                "error": "h2h_exception",
                "message": str(e)
            }
    
    async def create_token_payment(
        self,
        amount: Decimal,
        order_id: str,
        card_token: str,
        email: str,
        description: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Создает платеж по токену карты (только OBANK)
        
        Args:
            amount: Сумма платежа в сомах
            order_id: Уникальный ID заказа
            card_token: Токен сохраненной карты
            email: Email клиента
            description: Описание платежа
            
        Returns:
            Dict с результатом платежа
        """
        if self.provider != "OBANK":
            return {
                "success": False,
                "error": "token_not_supported",
                "message": f"Token платежи поддерживаются только провайдером OBANK, текущий: {self.provider}"
            }
        
        try:
            response = await self.service.create_token_payment(
                amount_kgs=float(amount),
                client_id=order_id,
                card_token=card_token
            )
            
            if response.get('success'):
                return {
                    "success": True,
                    "transaction_id": response.get('transaction_id'),
                    "auth_key": response.get('auth_key'),
                    "status": response.get('status', 'processing'),
                    "message": response.get('message', 'Token платеж создан'),
                    "provider": "OBANK"
                }
            else:
                return {
                    "success": False,
                    "error": response.get('error', 'token_payment_failed'),
                    "message": response.get('message', 'Ошибка создания Token платежа')
                }
                
        except Exception as e:
            logger.error(f"Token payment creation error: {e}")
            return {
                "success": False,
                "error": "token_exception", 
                "message": str(e)
            }
    
    async def create_token(self, days: int = 14) -> Dict[str, Any]:
        """
        Создает токен для сохранения карт (только OBANK)
        
        Args:
            days: Количество дней действия токена
            
        Returns:
            Dict с токеном
        """
        if self.provider != "OBANK":
            return {
                "success": False,
                "error": "token_creation_not_supported",
                "message": f"Создание токенов поддерживается только провайдером OBANK, текущий: {self.provider}"
            }
        
        try:
            response = await self.service.create_token(days=days)
            
            if response.get('success'):
                return {
                    "success": True,
                    "token_url": response.get('token_url'),
                    "token_expires_in_days": days,
                    "message": "Токен создан успешно",
                    "provider": "OBANK"
                }
            else:
                return {
                    "success": False,
                    "error": response.get('error', 'token_creation_failed'),
                    "message": response.get('message', 'Ошибка создания токена')
                }
                
        except Exception as e:
            logger.error(f"Token creation error: {e}")
            return {
                "success": False,
                "error": "token_creation_exception",
                "message": str(e)
            }
    
    async def check_h2h_status(self, auth_key: str) -> Dict[str, Any]:
        """
        Проверяет статус H2H платежа (только OBANK)
        
        Args:
            auth_key: Ключ аутентификации H2H платежа
            
        Returns:
            Dict со статусом платежа
        """
        if self.provider != "OBANK":
            return {
                "success": False,
                "error": "h2h_status_not_supported",
                "message": f"Проверка H2H статуса поддерживается только провайдером OBANK, текущий: {self.provider}"
            }
        
        try:
            response = await self.service.check_h2h_status(transaction_id=auth_key)
            
            return {
                "success": True,
                "status": response.get('status', 'processing'),
                "provider": "OBANK",
                "raw_response": response
            }
            
        except Exception as e:
            logger.error(f"H2H status check error: {e}")
            return {
                "success": False,
                "error": "h2h_status_exception",
                "message": str(e)
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
    """Получение сервиса с настройками по умолчанию"""
    return PaymentProviderService()

def get_qr_payment_service() -> PaymentProviderService:
    """Получение сервиса для QR платежей (O!Dengi)"""
    return PaymentProviderService(force_provider="ODENGI")

def get_card_payment_service() -> PaymentProviderService:
    """Получение сервиса для платежей картами (OBANK)"""
    return PaymentProviderService(force_provider="OBANK") 