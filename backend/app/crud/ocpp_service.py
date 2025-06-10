from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from app.db.models.ocpp import (
    OCPPStationStatus, 
    OCPPTransaction, 
    OCPPMeterValue, 
    OCPPAuthorization,
    OCPPConfiguration,
    Station,
    ChargingSession
)
import logging

logger = logging.getLogger(__name__)

class OCPPStationService:
    """Сервис для управления статусом станций OCPP"""
    
    @staticmethod
    def update_station_status(
        db: Session, 
        station_id: str, 
        status: str,
        error_code: str = None,
        info: str = None,
        vendor_id: str = None,
        vendor_error_code: str = None
    ) -> OCPPStationStatus:
        """Обновление статуса станции"""
        station_status = db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
        
        if not station_status:
            station_status = OCPPStationStatus(station_id=station_id)
            db.add(station_status)
        
        station_status.status = status
        station_status.error_code = error_code
        station_status.info = info
        station_status.vendor_id = vendor_id
        station_status.vendor_error_code = vendor_error_code
        station_status.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(station_status)
        return station_status
    
    @staticmethod
    def update_heartbeat(db: Session, station_id: str) -> OCPPStationStatus:
        """Обновляет время последнего heartbeat"""
        station_status = db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
        
        if not station_status:
            station_status = OCPPStationStatus(
                station_id=station_id,
                status="Available"
            )
            db.add(station_status)
        
        station_status.last_heartbeat = datetime.utcnow()
        station_status.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(station_status)
        return station_status
    
    @staticmethod
    def mark_boot_notification_sent(
        db: Session,
        station_id: str,
        firmware_version: str = None
    ) -> OCPPStationStatus:
        """Отмечает отправку BootNotification"""
        station_status = db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
        
        if not station_status:
            station_status = OCPPStationStatus(
                station_id=station_id,
                status="Available",
                firmware_version=firmware_version
            )
            db.add(station_status)
        else:
            if firmware_version:
                station_status.firmware_version = firmware_version
            station_status.status = "Available"
            station_status.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(station_status)
        return station_status
    
    @staticmethod
    def get_station_status(db: Session, station_id: str) -> Optional[OCPPStationStatus]:
        """Получает статус станции"""
        return db.query(OCPPStationStatus).filter(
            OCPPStationStatus.station_id == station_id
        ).first()
    
    @staticmethod
    def get_online_stations(db: Session) -> List[OCPPStationStatus]:
        """Получение списка онлайн станций"""
        cutoff_time = datetime.utcnow() - timedelta(minutes=10)
        return db.query(OCPPStationStatus).filter(
            OCPPStationStatus.is_online == True,
            OCPPStationStatus.last_heartbeat >= cutoff_time
        ).all()

class OCPPTransactionService:
    """Сервис для управления транзакциями OCPP"""
    
    @staticmethod
    def start_transaction(
        db: Session,
        station_id: str,
        transaction_id: int,
        connector_id: int,
        id_tag: str,
        meter_start: float,
        timestamp: datetime,
        charging_session_id: str = None
    ) -> OCPPTransaction:
        """Создание новой транзакции"""
        transaction = OCPPTransaction(
            transaction_id=transaction_id,
            station_id=station_id,
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=meter_start,
            start_timestamp=timestamp,
            charging_session_id=charging_session_id,
            status="Started"
        )
        
        db.add(transaction)
        db.commit()
        db.refresh(transaction)
        
        logger.info(f"OCPP Transaction started: {transaction_id} for station {station_id}")
        return transaction
    
    @staticmethod
    def stop_transaction(
        db: Session,
        station_id: str,
        transaction_id: int,
        meter_stop: float,
        timestamp: datetime,
        stop_reason: str = None
    ) -> Optional[OCPPTransaction]:
        """Завершение транзакции"""
        transaction = db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id,
            OCPPTransaction.transaction_id == transaction_id,
            OCPPTransaction.status == "Started"
        ).first()
        
        if not transaction:
            logger.error(f"Transaction {transaction_id} not found for station {station_id}")
            return None
        
        transaction.meter_stop = meter_stop
        transaction.stop_timestamp = timestamp
        transaction.stop_reason = stop_reason
        transaction.status = "Stopped"
        
        db.commit()
        db.refresh(transaction)
        
        logger.info(f"OCPP Transaction stopped: {transaction_id} for station {station_id}")
        return transaction
    
    @staticmethod
    def get_active_transaction(db: Session, station_id: str) -> Optional[OCPPTransaction]:
        """Получение активной транзакции для станции"""
        return db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id,
            OCPPTransaction.status == "Started"
        ).first()
    
    @staticmethod
    def get_transaction(
        db: Session, 
        station_id: str, 
        transaction_id: int
    ) -> Optional[OCPPTransaction]:
        """Получение транзакции по ID"""
        return db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id,
            OCPPTransaction.transaction_id == transaction_id
        ).first()

class OCPPMeterService:
    """Сервис для управления показаниями счетчиков"""
    
    @staticmethod
    def add_meter_values(
        db: Session,
        station_id: str,
        connector_id: int,
        timestamp: datetime,
        sampled_values: List[Dict[str, Any]],
        transaction_id: int = None
    ) -> OCPPMeterValue:
        """Добавление показаний счетчика"""
        
        # Находим OCPPTransaction объект по transaction_id
        ocpp_transaction_id = None
        if transaction_id:
            ocpp_transaction = db.query(OCPPTransaction).filter(
                OCPPTransaction.transaction_id == transaction_id,
                OCPPTransaction.station_id == station_id
            ).first()
            if ocpp_transaction:
                ocpp_transaction_id = ocpp_transaction.id
        
        # Парсим показания
        energy = None
        power = None
        current = None
        voltage = None
        temperature = None
        soc = None
        
        for sample in sampled_values:
            measurand = sample.get('measurand', '')
            value = sample.get('value')
            
            if value is not None:
                try:
                    if measurand == 'Energy.Active.Import.Register':
                        energy = float(value)
                    elif measurand == 'Power.Active.Import':
                        power = float(value)
                    elif measurand == 'Current.Import':
                        current = float(value)
                    elif measurand == 'Voltage':
                        voltage = float(value)
                    elif measurand == 'Temperature':
                        temperature = float(value)
                    elif measurand == 'SoC':
                        soc = float(value)
                except (ValueError, TypeError):
                    continue
        
        meter_value = OCPPMeterValue(
            transaction_id=transaction_id,  # OCPP transaction_id (не FK)
            ocpp_transaction_id=ocpp_transaction_id,  # FK к OCPPTransaction.id
            station_id=station_id,
            connector_id=connector_id,
            timestamp=timestamp,
            sampled_values=sampled_values,
            energy_active_import_register=energy,
            power_active_import=power,
            current_import=current,
            voltage=voltage,
            temperature=temperature,
            soc=soc
        )
        
        db.add(meter_value)
        db.commit()
        db.refresh(meter_value)
        
        return meter_value
    
    @staticmethod
    def get_latest_meter_values(
        db: Session, 
        station_id: str, 
        limit: int = 10
    ) -> List[OCPPMeterValue]:
        """Получение последних показаний счетчика"""
        return db.query(OCPPMeterValue).filter(
            OCPPMeterValue.station_id == station_id
        ).order_by(OCPPMeterValue.timestamp.desc()).limit(limit).all()

class OCPPAuthorizationService:
    """Сервис для управления авторизацией RFID/NFC"""
    
    @staticmethod
    def authorize_id_tag(db: Session, id_tag: str) -> Dict[str, str]:
        """Авторизация ID тега"""
        auth = db.query(OCPPAuthorization).filter(
            OCPPAuthorization.id_tag == id_tag
        ).first()
        
        if not auth:
            return {"status": "Invalid"}
        
        # Проверка срока действия
        if auth.expiry_date and auth.expiry_date <= datetime.utcnow():
            return {"status": "Expired"}
        
        return {"status": auth.status}
    
    @staticmethod
    def add_id_tag(
        db: Session,
        id_tag: str,
        status: str = "Accepted",
        user_id: str = None,
        expiry_date: datetime = None
    ) -> OCPPAuthorization:
        """Добавление нового ID тега"""
        auth = OCPPAuthorization(
            id_tag=id_tag,
            status=status,
            user_id=user_id,
            expiry_date=expiry_date
        )
        
        db.add(auth)
        db.commit()
        db.refresh(auth)
        
        return auth
    
    @staticmethod
    def get_user_by_id_tag(db: Session, id_tag: str) -> Optional[str]:
        """Получение user_id по ID тегу"""
        auth = db.query(OCPPAuthorization).filter(
            OCPPAuthorization.id_tag == id_tag,
            OCPPAuthorization.status == "Accepted"
        ).first()
        
        return auth.user_id if auth else None

class OCPPConfigurationService:
    """Сервис для управления конфигурацией OCPP"""
    
    @staticmethod
    def get_configuration(
        db: Session,
        station_id: str,
        keys: List[str] = None
    ) -> List[OCPPConfiguration]:
        """Получение конфигурационных параметров"""
        query = db.query(OCPPConfiguration).filter(
            OCPPConfiguration.station_id == station_id
        )
        
        if keys:
            query = query.filter(OCPPConfiguration.key.in_(keys))
        
        return query.all()
    
    @staticmethod
    def set_configuration(
        db: Session,
        station_id: str,
        key: str,
        value: str,
        readonly: bool = False
    ) -> OCPPConfiguration:
        """Установка конфигурационного параметра"""
        config = db.query(OCPPConfiguration).filter(
            OCPPConfiguration.station_id == station_id,
            OCPPConfiguration.key == key
        ).first()
        
        if not config:
            config = OCPPConfiguration(
                station_id=station_id,
                key=key,
                value=value,
                readonly=readonly
            )
            db.add(config)
        else:
            if not config.readonly:
                config.value = value
                config.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(config)
        return config
    
    @staticmethod
    def change_configuration(
        db: Session,
        station_id: str,
        key: str,
        value: str
    ) -> Dict[str, str]:
        """Изменение конфигурации с проверкой readonly"""
        config = db.query(OCPPConfiguration).filter(
            OCPPConfiguration.station_id == station_id,
            OCPPConfiguration.key == key
        ).first()
        
        if not config:
            # Создаем новый параметр
            config = OCPPConfiguration(
                station_id=station_id,
                key=key,
                value=value
            )
            db.add(config)
            db.commit()
            return {"status": "Accepted"}
        
        if config.readonly:
            return {"status": "Rejected"}
        
        # Валидация известных параметров
        known_configs = {
            "HeartbeatInterval": lambda v: v.isdigit() and 30 <= int(v) <= 3600,
            "MeterValueSampleInterval": lambda v: v.isdigit() and 5 <= int(v) <= 3600,
            "NumberOfConnectors": lambda v: v.isdigit() and 1 <= int(v) <= 10,
            "AuthorizeRemoteTxRequests": lambda v: v.lower() in ["true", "false"],
            "LocalAuthorizeOffline": lambda v: v.lower() in ["true", "false"],
            "TransactionMessageAttempts": lambda v: v.isdigit() and 1 <= int(v) <= 10
        }
        
        if key in known_configs:
            if not known_configs[key](value):
                return {"status": "Rejected"}
        
        config.value = value
        config.updated_at = datetime.utcnow()
        db.commit()
        
        return {"status": "Accepted"}

# ============================================================================
# O!DENGI ПЛАТЕЖНЫЙ СЕРВИС
# ============================================================================

import httpx
import hmac
import hashlib
import time
from typing import Dict, Any, Optional
from app.core.config import settings
from app.schemas.ocpp import PaymentWebhookData
from sqlalchemy import text
from decimal import Decimal

class ODengiService:
    """Сервис для работы с O!Dengi JSON API"""
    
    def __init__(self):
        # Инициализация отложенная до первого использования
        self._api_url = None
        self._merchant_id = None
        self._password = None
        self._use_production = None
        self.api_version = 1005  # Версия API из документации
        self._initialized = False
    
    def _ensure_initialized(self):
        """Ленивая инициализация настроек"""
        if not self._initialized:
            self._api_url = (
                settings.ODENGI_PRODUCTION_API_URL 
                if settings.ODENGI_USE_PRODUCTION 
                else settings.ODENGI_API_URL
            )
            
            # Выбираем правильные креды в зависимости от окружения
            if settings.ODENGI_USE_PRODUCTION:
                self._merchant_id = settings.ODENGI_PROD_MERCHANT_ID or settings.ODENGI_MERCHANT_ID
                self._password = settings.ODENGI_PROD_PASSWORD or settings.ODENGI_PASSWORD
            else:
                self._merchant_id = settings.ODENGI_MERCHANT_ID
                self._password = settings.ODENGI_PASSWORD
                
            self._use_production = settings.ODENGI_USE_PRODUCTION
            
            # Проверка production конфигурации
            if self._use_production and (not self._merchant_id or not self._password):
                logger.warning("⚠️ Production режим включен, но отсутствуют production креды O!Dengi!")
            
            self._initialized = True
    
    @property
    def api_url(self):
        self._ensure_initialized()
        return self._api_url
    
    @property
    def merchant_id(self):
        self._ensure_initialized()
        return self._merchant_id
    
    @property
    def password(self):
        self._ensure_initialized()
        return self._password
    
    @property
    def use_production(self):
        self._ensure_initialized()
        return self._use_production
    
    def generate_secure_order_id(self, payment_type: str, client_id: str, **kwargs) -> str:
        """Генерация безопасного order_id"""
        timestamp = int(time.time())
        
        if payment_type == "topup":
            data = f"TOPUP_{client_id}_{timestamp}"
        elif payment_type == "charging":
            station_id = kwargs.get('station_id', '')
            connector_id = kwargs.get('connector_id', 1)
            data = f"CHARGE_{station_id}_{connector_id}_{timestamp}_{client_id}"
        else:
            data = f"PAYMENT_{client_id}_{timestamp}"
        
        signature = hmac.new(
            settings.EZS_SECRET_KEY.encode(),
            data.encode(),
            hashlib.sha256
        ).hexdigest()[:8]
        
        return f"{data}_{signature}"
    
    def validate_order_id(self, order_id: str) -> bool:
        """Валидация order_id для предотвращения мошенничества"""
        try:
            parts = order_id.split('_')
            if len(parts) < 4:
                return False
            
            # Получаем подпись (последняя часть)
            signature = parts[-1]
            # Восстанавливаем исходные данные
            data = '_'.join(parts[:-1])
            
            expected_signature = hmac.new(
                settings.EZS_SECRET_KEY.encode(),
                data.encode(),
                hashlib.sha256
            ).hexdigest()[:8]
            
            return signature == expected_signature
        except Exception:
            return False
    
    def generate_hash(self, request_data: Dict[str, Any]) -> str:
        """Генерация hash подписи для запроса O!Dengi с использованием HMAC-MD5"""
        import json
        
        # Создаем JSON в точном порядке как в документации
        ordered_data = {
            "cmd": request_data["cmd"],
            "version": request_data["version"], 
            "sid": request_data["sid"],
            "mktime": request_data["mktime"],
            "lang": request_data["lang"],
            "data": request_data["data"]
        }
        
        # Сериализуем JSON без пробелов и БЕЗ сортировки ключей
        json_string = json.dumps(ordered_data, separators=(',', ':'), ensure_ascii=False, sort_keys=False)
        
        # Генерируем HMAC-MD5 подпись с паролем как ключом
        return hmac.new(
            self.password.encode('utf-8'),
            json_string.encode('utf-8'),
            hashlib.md5
        ).hexdigest()
    
    async def create_invoice(
        self, 
        order_id: str, 
        description: str, 
        amount_kopecks: int,
        **kwargs
    ) -> Dict[str, Any]:
        """Создание счета в O!Dengi через JSON API"""
        
        current_time = int(time.time())
        
        request_data = {
            "cmd": "createInvoice",
            "version": self.api_version,
            "sid": self.merchant_id,
            "mktime": str(current_time),
            "lang": "ru",
            "data": {
                "amount": amount_kopecks,
                "desc": description,
                "order_id": order_id
            }
        }
        
        # Генерируем подпись
        request_data["hash"] = self.generate_hash(request_data)
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.post(
                    self.api_url,
                    json=request_data,
                    headers={"Content-Type": "application/json; charset=utf-8"}
                )
                response.raise_for_status()
                
                result = response.json()
                logger.info(f"O!Dengi createInvoice response: {result}")
                
                return result
                
        except Exception as e:
            logger.error(f"O!Dengi createInvoice error: {e}")
            raise
    
    async def get_payment_status(self, invoice_id: str, order_id: Optional[str] = None) -> Dict[str, Any]:
        """Проверка статуса платежа"""
        
        current_time = int(time.time())
        
        request_data = {
            "cmd": "statusPayment",
            "version": self.api_version,
            "sid": self.merchant_id,
            "mktime": str(current_time),
            "lang": "ru",
            "data": {
                "invoice_id": invoice_id
            }
        }
        
        if order_id:
            request_data["data"]["order_id"] = order_id
        
        # Генерируем подпись
        request_data["hash"] = self.generate_hash(request_data)
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.post(
                    self.api_url,
                    json=request_data,
                    headers={"Content-Type": "application/json; charset=utf-8"}
                )
                response.raise_for_status()
                
                result = response.json()
                logger.info(f"O!Dengi statusPayment response: {result}")
                
                return result
                
        except Exception as e:
            logger.error(f"O!Dengi statusPayment error: {e}")
            raise
    
    def verify_webhook_signature(self, payload: bytes, received_signature: str) -> bool:
        """Верификация подписи webhook"""
        if not settings.ODENGI_WEBHOOK_SECRET:
            logger.warning("ODENGI_WEBHOOK_SECRET not configured, skipping signature verification")
            return True
        
        expected_signature = hmac.new(
            settings.ODENGI_WEBHOOK_SECRET.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        # Поддержка формата "sha256=signature"
        if received_signature.startswith('sha256='):
            received_signature = received_signature[7:]
        
        return hmac.compare_digest(expected_signature, received_signature)
    
    @staticmethod
    def get_status_text(status: int) -> str:
        """Преобразование статуса O!Dengi в текст"""
        statuses = {
            0: "Ожидает оплаты",
            1: "Оплачено", 
            2: "Отменено",
            3: "Возврат",
            4: "Частичный возврат"
        }
        return statuses.get(status, "Неизвестный статус")
    
    @staticmethod
    def can_proceed(status: int) -> bool:
        """Проверка возможности проведения операции"""
        return status == 1  # Только если оплачено

class PaymentService:
    """Сервис для работы с балансом и платежами"""
    
    @staticmethod
    def get_client_balance(db: Session, client_id: str) -> Decimal:
        """Получение текущего баланса клиента"""
        result = db.execute(text("""
            SELECT balance FROM clients WHERE id = :client_id
        """), {"client_id": client_id})
        
        row = result.fetchone()
        return Decimal(str(row[0])) if row else Decimal('0')
    
    @staticmethod
    def update_client_balance(
        db: Session, 
        client_id: str, 
        amount: Decimal, 
        operation: str = "add",
        description: str = ""
    ) -> Decimal:
        """Обновление баланса клиента с логированием"""
        
        # Получаем текущий баланс
        current_balance = PaymentService.get_client_balance(db, client_id)
        
        # Вычисляем новый баланс
        if operation == "add":
            new_balance = current_balance + amount
        elif operation == "subtract":
            new_balance = current_balance - amount
            if new_balance < 0:
                raise ValueError("Недостаточно средств на балансе")
        else:
            raise ValueError("Неподдерживаемая операция")
        
        # Обновляем баланс
        db.execute(text("""
            UPDATE clients 
            SET balance = :new_balance, updated_at = NOW() 
            WHERE id = :client_id
        """), {"new_balance": new_balance, "client_id": client_id})
        
        logger.info(f"Баланс клиента {client_id}: {current_balance} -> {new_balance} ({operation} {amount})")
        
        return new_balance
    
    @staticmethod
    def create_payment_transaction(
        db: Session,
        client_id: str,
        transaction_type: str,
        amount: Decimal,
        balance_before: Decimal,
        balance_after: Decimal,
        description: str = "",
        **kwargs
    ) -> int:
        """Создание записи о транзакции"""
        
        insert_data = {
            "client_id": client_id,
            "transaction_type": transaction_type,
            "amount": amount,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "description": description,
            "balance_topup_id": kwargs.get('balance_topup_id'),
            "charging_session_id": kwargs.get('charging_session_id')
        }
        
        result = db.execute(text("""
            INSERT INTO payment_transactions_odengi 
            (client_id, transaction_type, amount, balance_before, balance_after, 
             description, balance_topup_id, charging_session_id)
            VALUES (:client_id, :transaction_type, :amount, :balance_before, :balance_after,
                    :description, :balance_topup_id, :charging_session_id)
            RETURNING id
        """), insert_data)
        
        transaction_id = result.fetchone()[0]
        logger.info(f"Создана транзакция {transaction_id}: {description}")
        
        return transaction_id
    
    @staticmethod
    def check_sufficient_balance(db: Session, client_id: str, amount: Decimal) -> bool:
        """Проверка достаточности средств"""
        current_balance = PaymentService.get_client_balance(db, client_id)
        return current_balance >= amount

# Синглтоны для использования в приложении
odengi_service = ODengiService()
payment_service = PaymentService()

class PaymentLifecycleService:
    """Сервис для управления временем жизни платежей и status check"""
    
    QR_LIFETIME_MINUTES = 5  # QR код живет 5 минут
    INVOICE_LIFETIME_MINUTES = 5  # Invoice живет 5 минут
    STATUS_CHECK_INTERVAL_SECONDS = 15  # Проверка статуса каждые 15 секунд
    MAX_STATUS_CHECKS = 20  # Максимум 20 проверок (5 минут)
    
    @staticmethod
    def calculate_expiry_times(created_at: datetime) -> tuple[datetime, datetime]:
        """Рассчитывает время истечения QR кода и invoice"""
        qr_expires_at = created_at + timedelta(minutes=PaymentLifecycleService.QR_LIFETIME_MINUTES)
        invoice_expires_at = created_at + timedelta(minutes=PaymentLifecycleService.INVOICE_LIFETIME_MINUTES)
        return qr_expires_at, invoice_expires_at
    
    @staticmethod
    def is_qr_expired(qr_expires_at: datetime) -> bool:
        """Проверяет истек ли QR код"""
        now = datetime.now(timezone.utc)
        if qr_expires_at.tzinfo is None:
            qr_expires_at = qr_expires_at.replace(tzinfo=timezone.utc)
        return now > qr_expires_at
    
    @staticmethod
    def is_invoice_expired(invoice_expires_at: datetime) -> bool:
        """Проверяет истек ли invoice"""
        now = datetime.now(timezone.utc)
        if invoice_expires_at.tzinfo is None:
            invoice_expires_at = invoice_expires_at.replace(tzinfo=timezone.utc)
        return now > invoice_expires_at
    
    @staticmethod
    def should_status_check(payment_created_at: datetime, last_check_at: Optional[datetime], 
                           check_count: int, payment_status: str) -> bool:
        """Определяет нужна ли проверка статуса"""
        # Не проверяем завершенные платежи
        if payment_status in ['paid', 'cancelled', 'refunded']:
            return False
        
        # Превышен лимит проверок
        if check_count >= PaymentLifecycleService.MAX_STATUS_CHECKS:
            return False
        
        # Invoice истек
        _, invoice_expires_at = PaymentLifecycleService.calculate_expiry_times(payment_created_at)
        if PaymentLifecycleService.is_invoice_expired(invoice_expires_at):
            return False
        
        # Первая проверка или прошло достаточно времени
        if last_check_at is None:
            return True
        
        next_check_time = last_check_at + timedelta(seconds=PaymentLifecycleService.STATUS_CHECK_INTERVAL_SECONDS)
        now = datetime.now(timezone.utc)
        if next_check_time.tzinfo is None:
            next_check_time = next_check_time.replace(tzinfo=timezone.utc)
        return now >= next_check_time
    
    @staticmethod
    async def perform_status_check(db: Session, payment_table: str, invoice_id: str) -> dict:
        """Выполняет проверку статуса платежа через выбранный платежный провайдер"""
        try:
            # Получаем данные платежа (включая paid_amount для проверки дублирования)
            if payment_table == "balance_topups":
                query = text("""
                    SELECT id, order_id, client_id, status, status_check_count, created_at, paid_amount
                    FROM balance_topups WHERE invoice_id = :invoice_id
                """)
            else:  # charging_payments
                query = text("""
                    SELECT id, order_id, client_id, status, status_check_count, created_at, paid_amount
                    FROM charging_payments WHERE invoice_id = :invoice_id
                """)
            
            result = db.execute(query, {"invoice_id": invoice_id}).fetchone()
            if not result:
                return {"success": False, "error": "payment_not_found"}
            
            payment_id, order_id, client_id, current_status, check_count, created_at, existing_paid_amount = result
            
            # Проверяем нужна ли проверка
            if not PaymentLifecycleService.should_status_check(created_at, None, check_count, current_status):
                return {"success": False, "error": "status_check_not_needed"}
            
            # Выбираем провайдера и вызываем соответствующий API
            if settings.PAYMENT_PROVIDER == "OBANK":
                # Для OBANK используем auth_key из order_id
                from app.services.obank_service import obank_service
                api_response = await obank_service.check_payment_status(auth_key=invoice_id)
                
                # Парсим ответ OBANK
                obank_status = api_response.get('data', {}).get('status', 'processing')
                # Маппинг статусов OBANK: processing, completed, failed, cancelled
                status_mapping = {
                    'processing': "processing",
                    'completed': "approved", 
                    'failed': "canceled",
                    'cancelled': "canceled"
                }
                mapped_status = status_mapping.get(obank_status, "processing")
                new_status = 1 if mapped_status == "approved" else 0 if mapped_status == "processing" else 2
                paid_amount = float(api_response.get('data', {}).get('sum', 0)) / 1000 if mapped_status == "approved" else None
                
            else:  # O!Dengi (Legacy)
                # Вызываем O!Dengi API
                odengi_response = await odengi_service.get_payment_status(
                    invoice_id=invoice_id,
                    order_id=order_id
                )
                
                # Парсим ответ O!Dengi правильно
                data = odengi_response.get('data', {})
                
                # Проверяем есть ли payments (для approved статуса)
                if 'payments' in data and data['payments']:
                    payment_info = data['payments'][0]  # Берем первый платеж
                    payment_status = payment_info.get('status')
                    paid_amount = float(payment_info.get('amount', 0)) / 100  # Из копеек в сомы
                    
                    # Маппинг статусов от O!Dengi
                    if payment_status == 'approved':
                        new_status = 1
                        mapped_status = "approved"
                    elif payment_status == 'processing':
                        new_status = 0
                        mapped_status = "processing"
                    else:
                        new_status = 2
                        mapped_status = "canceled"
                else:
                    # Fallback для случая без payments (обычно processing)
                    status_text = data.get('status', 'processing')
                    if status_text == 'processing':
                        new_status = 0
                        mapped_status = "processing"
                        paid_amount = None
                    else:
                        new_status = 2
                        mapped_status = "canceled"
                        paid_amount = None
            
            # Если платеж оплачен - обрабатываем ПЕРЕД обновлением статуса
            payment_processed = False
            if new_status == 1 and current_status != "approved" and existing_paid_amount is None:
                # КРИТИЧЕСКИ ВАЖНО: Проверяем что платеж еще не был обработан
                if payment_table == "balance_topups":
                    # Обрабатываем пополнение баланса
                    current_balance = payment_service.get_client_balance(db, client_id)
                    new_balance = payment_service.update_client_balance(
                        db, client_id, Decimal(str(paid_amount or 0)), "add",
                        f"Пополнение баланса через O!Dengi (invoice: {invoice_id})"
                    )
                    
                    # Создаем транзакцию
                    payment_service.create_payment_transaction(
                        db, client_id, "balance_topup", 
                        Decimal(str(paid_amount or 0)), current_balance, new_balance,
                        f"Пополнение баланса через O!Dengi",
                        balance_topup_id=payment_id
                    )
                    
                    payment_processed = True
                    logger.info(f"✅ Баланс пополнен автоматически: клиент {client_id}, сумма {paid_amount}, новый баланс {new_balance}")
                # Для charging_payments логика обработки отдельно
            elif new_status == 1 and existing_paid_amount is not None:
                # Платеж уже был обработан ранее
                logger.info(f"⚠️ Платеж {invoice_id} уже был обработан ранее (paid_amount: {existing_paid_amount})")
                payment_processed = False
            
            # Обновляем статус в базе (включая paid_at если платеж обработан)
            if payment_processed and payment_table == "balance_topups":
                update_query = text("""
                    UPDATE balance_topups 
                    SET last_status_check_at = NOW(), 
                        status_check_count = status_check_count + 1,
                        odengi_status = :odengi_status,
                        status = :status,
                        paid_amount = :paid_amount,
                        paid_at = NOW(),
                        needs_status_check = :needs_check
                    WHERE invoice_id = :invoice_id
                """)
            elif payment_table == "balance_topups":
                update_query = text("""
                    UPDATE balance_topups 
                    SET last_status_check_at = NOW(), 
                        status_check_count = status_check_count + 1,
                        odengi_status = :odengi_status,
                        status = :status,
                        paid_amount = :paid_amount,
                        needs_status_check = :needs_check
                    WHERE invoice_id = :invoice_id
                """)
            else:  # charging_payments
                update_query = text("""
                    UPDATE charging_payments 
                    SET last_status_check_at = NOW(), 
                        status_check_count = status_check_count + 1,
                        odengi_status = :odengi_status,
                        status = :status,
                        paid_amount = :paid_amount,
                        needs_status_check = :needs_check
                    WHERE invoice_id = :invoice_id
                """)
            
            # Определяем нужны ли дальнейшие проверки
            needs_further_checks = mapped_status == "processing" and check_count < PaymentLifecycleService.MAX_STATUS_CHECKS
            
            db.execute(update_query, {
                "odengi_status": new_status,
                "status": mapped_status,
                "paid_amount": paid_amount,
                "needs_check": needs_further_checks,
                "invoice_id": invoice_id
            })
            
            db.commit()
            
            logger.info(f"Status check completed for {invoice_id}: {current_status} -> {mapped_status}")
            
            return {
                "success": True,
                "old_status": current_status,
                "new_status": mapped_status,
                "odengi_status": new_status,
                "paid_amount": paid_amount,
                "needs_further_checks": needs_further_checks
            }
            
        except Exception as e:
            logger.error(f"Status check failed for {invoice_id}: {e}")
            db.rollback()
            return {"success": False, "error": str(e)}
    
    @staticmethod
    async def cleanup_expired_payments(db: Session) -> dict:
        """Очищает просроченные платежи и устанавливает статус cancelled"""
        try:
            current_time = datetime.now(timezone.utc)
            
            # Отменяем просроченные пополнения баланса
            try:
                topup_update = text("""
                    UPDATE balance_topups 
                    SET status = 'canceled', 
                        needs_status_check = false,
                        completed_at = NOW()
                    WHERE status = 'processing' 
                      AND invoice_expires_at < :current_time
                      AND needs_status_check = true
                """)
                
                topup_result = db.execute(topup_update, {"current_time": current_time})
            except UnicodeDecodeError as e:
                logger.error(f"Unicode error in cleanup topups, skipping: {e}")
                topup_result = type('MockResult', (), {'rowcount': 0})()
            
            # Отменяем просроченные платежи за зарядку
            try:
                charging_update = text("""
                    UPDATE charging_payments 
                    SET status = 'canceled',
                        needs_status_check = false, 
                        completed_at = NOW()
                    WHERE status = 'processing'
                      AND invoice_expires_at < :current_time
                      AND needs_status_check = true
                """)
                
                charging_result = db.execute(charging_update, {"current_time": current_time})
            except UnicodeDecodeError as e:
                logger.error(f"Unicode error in cleanup charging, skipping: {e}")
                charging_result = type('MockResult', (), {'rowcount': 0})()
            
            db.commit()
            
            logger.info(f"Expired payments cleanup: {topup_result.rowcount} topups, {charging_result.rowcount} charging payments cancelled")
            
            return {
                "success": True,
                "cancelled_topups": topup_result.rowcount,
                "cancelled_charging_payments": charging_result.rowcount
            }
            
        except Exception as e:
            logger.error(f"Cleanup expired payments failed: {e}")
            db.rollback()
            return {"success": False, "error": str(e)}

# Создаем экземпляр сервиса
payment_lifecycle_service = PaymentLifecycleService() 