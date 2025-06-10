"""
OBANK Payment Link API Service

Сервис для интеграции с OBANK Payment API для обработки платежей
через электронные кошельки и банковские карты.

Поддерживает:
- Создание платежных ссылок (Payment Page)
- Host2Host платежи
- Проверка статуса платежей
- Сохранение карт и токен-платежи
- Отмена и возврат средств
"""

import ssl
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional, Union
from datetime import datetime, timezone
import httpx
import logging
from decimal import Decimal

from app.core.config import settings

logger = logging.getLogger(__name__)

class OBankPaymentError(Exception):
    """Базовое исключение для ошибок OBANK API"""
    pass

class OBankAuthenticationError(OBankPaymentError):
    """Ошибка аутентификации OBANK"""
    pass

class OBankAPIError(OBankPaymentError):
    """Ошибка вызова OBANK API"""
    pass

class OBankService:
    """Сервис для работы с OBANK Payment Link API"""
    
    def __init__(self):
        # Инициализация отложенная до первого использования
        self._api_url = None
        self._point_id = None
        self._service_id = None
        self._cert_path = None
        self._cert_password = None
        self._use_production = None
        self._ssl_context = None
        self._initialized = False
    
    def _ensure_initialized(self):
        """Ленивая инициализация настроек"""
        if not self._initialized:
            self._api_url = settings.current_obank_api_url
            self._point_id = settings.current_obank_point_id
            self._service_id = settings.current_obank_service_id
            self._cert_path = settings.OBANK_CERT_PATH
            self._cert_password = settings.OBANK_CERT_PASSWORD
            self._use_production = settings.OBANK_USE_PRODUCTION
            self._initialized = True
    
    @property
    def api_url(self):
        self._ensure_initialized()
        return self._api_url
    
    @property
    def point_id(self):
        self._ensure_initialized()
        return self._point_id
    
    @property
    def service_id(self):
        self._ensure_initialized()
        return self._service_id
    
    @property
    def cert_path(self):
        self._ensure_initialized()
        return self._cert_path
    
    @property
    def cert_password(self):
        self._ensure_initialized()
        return self._cert_password
    
    @property
    def use_production(self):
        self._ensure_initialized()
        return self._use_production
    
    @property
    def ssl_context(self) -> ssl.SSLContext:
        """Ленивая загрузка SSL контекста"""
        if self._ssl_context is None:
            self._ssl_context = self._create_ssl_context()
        return self._ssl_context
    
    def _create_ssl_context(self) -> ssl.SSLContext:
        """Создает SSL контекст с клиентским сертификатом PKCS12"""
        try:
            context = ssl.create_default_context()
            
            # Загружаем PKCS12 сертификат только если файл существует
            if self.cert_path and self.cert_password and not self.cert_path.startswith('/path/to/'):
                import os
                if os.path.exists(self.cert_path):
                    context.load_cert_chain(self.cert_path, password=self.cert_password)
                    logger.info(f"SSL сертификат загружен: {self.cert_path}")
                else:
                    logger.warning(f"SSL сертификат не найден: {self.cert_path}")
                    if self.use_production:
                        raise OBankAuthenticationError(f"SSL сертификат обязателен для production: {self.cert_path}")
            else:
                logger.info("SSL сертификат не настроен - работаем без клиентского сертификата")
            
            # Отключаем проверку сертификата для тестового окружения
            if not self.use_production:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                logger.info("Тестовый режим: SSL проверки отключены")
            
            return context
            
        except Exception as e:
            logger.error(f"Ошибка создания SSL контекста: {e}")
            # В тестовом режиме создаем базовый контекст без сертификата
            if not self.use_production:
                logger.warning("Создаем базовый SSL контекст для тестового режима")
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                return context
            raise OBankAuthenticationError(f"Не удалось загрузить SSL сертификат: {e}")
    
    def _build_xml_request(self, method_data: Dict[str, Any], payment_data: Optional[Dict[str, Any]] = None) -> str:
        """Формирует XML запрос для OBANK API"""
        root = ET.Element("request", point=self.point_id)
        
        if payment_data:
            # Для платежных операций
            payment_elem = ET.SubElement(root, "payment", payment_data)
            
            for key, value in method_data.items():
                attr_elem = ET.SubElement(payment_elem, "attribute", name=key, value=str(value))
        else:
            # Для служебных операций (статус, отмена и т.д.)
            if "function" in method_data:
                # Advanced функции
                advanced_elem = ET.SubElement(root, "advanced", 
                    service=self.service_id, 
                    function=method_data["function"]
                )
                
                for key, value in method_data.items():
                    if key != "function":
                        attr_elem = ET.SubElement(advanced_elem, "attribute", name=key, value=str(value))
            elif "id" in method_data and "sum" in method_data:
                # Отмена операции
                cancel_elem = ET.SubElement(root, "cancel", 
                    id=str(method_data["id"]),
                    sum=str(method_data["sum"])
                )
            elif "id" in method_data:
                # Запрос статуса
                status_elem = ET.SubElement(root, "status", id=str(method_data["id"]))
        
        return ET.tostring(root, encoding='unicode')
    
    def _parse_xml_response(self, xml_data: str) -> Dict[str, Any]:
        """Парсит XML ответ от OBANK API"""
        try:
            root = ET.fromstring(xml_data)
            
            result = {}
            
            # Извлекаем атрибуты result
            result_elem = root.find('result')
            if result_elem is not None:
                result.update(result_elem.attrib)
                
                # Извлекаем data/input элементы
                data_elem = result_elem.find('data')
                if data_elem is not None:
                    inputs = {}
                    for input_elem in data_elem.findall('input'):
                        key = input_elem.get('key')
                        value = input_elem.get('value')
                        if key and value:
                            inputs[key] = value
                    result['data'] = inputs
            
            return result
            
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML ответа: {e}")
            raise OBankAPIError(f"Некорректный XML ответ: {e}")
    
    async def _make_request(self, xml_data: str, endpoint: str = "") -> Dict[str, Any]:
        """Выполняет HTTP запрос к OBANK API"""
        url = f"{self.api_url}/{endpoint}" if endpoint else self.api_url
        
        headers = {
            "Content-Type": "application/xml",
            "Accept": "application/xml"
        }
        
        try:
            async with httpx.AsyncClient(verify=self.ssl_context) as client:
                logger.info(f"Отправка запроса в OBANK: {url}")
                logger.debug(f"XML запрос: {xml_data}")
                
                response = await client.post(
                    url,
                    content=xml_data,
                    headers=headers,
                    timeout=30.0
                )
                
                response.raise_for_status()
                
                logger.debug(f"XML ответ: {response.text}")
                return self._parse_xml_response(response.text)
                
        except httpx.RequestError as e:
            logger.error(f"Ошибка HTTP запроса к OBANK: {e}")
            raise OBankAPIError(f"Ошибка соединения с OBANK: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP ошибка от OBANK: {e.response.status_code} - {e.response.text}")
            raise OBankAPIError(f"OBANK вернул ошибку: {e.response.status_code}")
    
    async def create_payment_page(
        self,
        amount: Decimal,
        order_id: str,
        email: str,
        notify_url: str,
        redirect_url: str,
        phone_number: Optional[str] = None,
        address: Optional[str] = None,
        city: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Создает платежную страницу для оплаты
        
        Args:
            amount: Сумма платежа в сомах
            order_id: Уникальный ID заказа
            email: Email клиента
            notify_url: URL для уведомлений
            redirect_url: URL для редиректа
            phone_number: Телефон клиента (опционально)
            address: Адрес клиента (опционально)
            city: Город клиента (опционально)
            
        Returns:
            Dict с данными платежной ссылки
        """
        # Конвертируем сумму в тыйыны (1 сом = 1000 тыйынов)
        amount_in_tyiyn = int(amount * 1000)
        
        method_data = {
            "sum": amount_in_tyiyn,
            "amount_currency": "417",  # KGS код валюты
            "notify_url": notify_url,
            "redirect_url": redirect_url,
            "email": email,
            "order_id": order_id,
            "function": "auth-acquiring"
        }
        
        # Добавляем опциональные поля
        if phone_number:
            method_data["phone_number"] = phone_number
        if address:
            method_data["address"] = address
        if city:
            method_data["city"] = city
        if kwargs.get("province"):
            method_data["province"] = kwargs["province"]
        if kwargs.get("post_code"):
            method_data["post_code"] = kwargs["post_code"]
        if kwargs.get("country_code"):
            method_data["country_code"] = kwargs["country_code"]
        
        xml_request = self._build_xml_request(method_data)
        response = await self._make_request(xml_request, "PaymentPage")
        
        # Проверяем успешность ответа
        if response.get("code") != "0":
            raise OBankAPIError(f"Ошибка создания платежной страницы: {response}")
        
        return response
    
    async def check_payment_status(self, auth_key: str) -> Dict[str, Any]:
        """
        Проверяет статус платежа по auth-key
        
        Args:
            auth_key: Ключ аутентификации платежа
            
        Returns:
            Dict со статусом платежа
        """
        method_data = {
            "function": "fetch-operation",
            "key": auth_key
        }
        
        xml_request = self._build_xml_request(method_data)
        response = await self._make_request(xml_request, "status")
        
        return response
    
    async def create_h2h_payment(
        self,
        amount: Decimal,
        transaction_id: str,
        account: str,
        email: str,
        notify_url: str,
        redirect_url: str,
        card_pan: str,
        card_name: str,
        card_cvv: str,
        card_year: str,
        card_month: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Создает Host2Host платеж с данными карты
        
        Args:
            amount: Сумма платежа в сомах
            transaction_id: ID транзакции
            account: Номер карты/счета
            email: Email клиента
            notify_url: URL для уведомлений
            redirect_url: URL для редиректа
            card_pan: Номер карты
            card_name: Имя владельца карты
            card_cvv: CVV код
            card_year: Год истечения карты
            card_month: Месяц истечения карты
            
        Returns:
            Dict с результатом платежа
        """
        # Конвертируем сумму в тыйыны
        amount_in_tyiyn = int(amount * 1000)
        
        # Данные платежа
        payment_data = {
            "id": transaction_id,
            "sum": str(amount_in_tyiyn),
            "check": "0",
            "service": self.service_id,
            "date": datetime.now(timezone.utc).isoformat(),
            "account": account
        }
        
        # Атрибуты платежа
        method_data = {
            "amount_currency": "417",  # KGS
            "notify_url": notify_url,
            "redirect_url": redirect_url,
            "card_pan": card_pan,
            "card_name": card_name,
            "card_cvv": card_cvv,
            "card_year": card_year,
            "card_month": card_month,
            "email": email
        }
        
        # Добавляем опциональные поля
        for key in ["phone_number", "address", "city", "province", "post_code", "country_code"]:
            if kwargs.get(key):
                method_data[key] = kwargs[key]
        
        xml_request = self._build_xml_request(method_data, payment_data)
        response = await self._make_request(xml_request, "h2h-payment")
        
        return response
    
    async def check_h2h_status(self, transaction_id: str) -> Dict[str, Any]:
        """
        Проверяет статус H2H платежа
        
        Args:
            transaction_id: ID транзакции
            
        Returns:
            Dict со статусом платежа
        """
        method_data = {"id": transaction_id}
        
        xml_request = self._build_xml_request(method_data)
        response = await self._make_request(xml_request, "h2hstatus")
        
        return response
    
    async def create_token(self, days: int = 14) -> Dict[str, Any]:
        """
        Создает токен для сохранения карт
        
        Args:
            days: Количество дней для действия токена (максимум 14)
            
        Returns:
            Dict с токенами карт
        """
        method_data = {
            "function": "stored-cards",
            "days": min(days, 14)  # Ограничиваем максимумом 14 дней
        }
        
        xml_request = self._build_xml_request(method_data)
        response = await self._make_request(xml_request, "token-Create")
        
        return response
    
    async def create_token_payment(
        self,
        amount: Decimal,
        transaction_id: str,
        email: str,
        notify_url: str,
        redirect_url: str,
        card_token: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Создает платеж по сохраненному токену карты
        
        Args:
            amount: Сумма платежа в сомах
            transaction_id: ID транзакции
            email: Email клиента
            notify_url: URL для уведомлений
            redirect_url: URL для редиректа
            card_token: Токен сохраненной карты
            
        Returns:
            Dict с результатом платежа
        """
        # Конвертируем сумму в тыйыны
        amount_in_tyiyn = int(amount * 1000)
        
        # Данные платежа
        payment_data = {
            "id": transaction_id,
            "sum": str(amount_in_tyiyn),
            "check": "0",
            "service": self.service_id,
            "date": datetime.now(timezone.utc).isoformat(),
            "account": ""  # Пустой для токен-платежей
        }
        
        # Атрибуты платежа
        method_data = {
            "amount_currency": "417",  # KGS
            "notify_url": notify_url,
            "redirect_url": redirect_url,
            "email": email,
            "card-token": card_token
        }
        
        # Добавляем опциональные поля
        for key in ["phone_number", "address", "city", "province", "post_code", "country_code"]:
            if kwargs.get(key):
                method_data[key] = kwargs[key]
        
        xml_request = self._build_xml_request(method_data, payment_data)
        response = await self._make_request(xml_request, "token-payment")
        
        return response
    
    async def cancel_payment(self, transaction_id: str, refund_amount: Decimal) -> Dict[str, Any]:
        """
        Отменяет платеж и возвращает средства
        
        Args:
            transaction_id: ID транзакции для отмены
            refund_amount: Сумма возврата в сомах
            
        Returns:
            Dict с результатом отмены
        """
        # Конвертируем сумму в тыйыны
        refund_amount_in_tyiyn = int(refund_amount * 1000)
        
        method_data = {
            "id": transaction_id,
            "sum": refund_amount_in_tyiyn
        }
        
        xml_request = self._build_xml_request(method_data)
        response = await self._make_request(xml_request, "Reversal")
        
        return response

# Создаем глобальный экземпляр сервиса
obank_service = OBankService() 