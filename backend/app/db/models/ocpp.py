from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Enum as SqlEnum, Boolean, Integer, Text, Numeric, ARRAY, JSON
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import enum
import uuid
from app.db.base_class import Base

# Enums
class UserRole(str, enum.Enum):
    operator = 'operator'
    admin = 'admin'
    superadmin = 'superadmin'

class ClientStatus(str, enum.Enum):
    active = 'active'
    inactive = 'inactive'
    blocked = 'blocked'

class StationStatus(str, enum.Enum):
    active = 'active'
    inactive = 'inactive'
    maintenance = 'maintenance'

class MaintenanceStatus(str, enum.Enum):
    pending = 'pending'
    in_progress = 'in_progress'
    completed = 'completed'
    cancelled = 'cancelled'

class ChargingSessionStatus(str, enum.Enum):
    started = 'started'
    stopped = 'stopped'
    error = 'error'

class LimitType(str, enum.Enum):
    none = 'none'
    energy = 'energy'
    amount = 'amount'

class TariffType(str, enum.Enum):
    per_kwh = 'per_kwh'
    per_minute = 'per_minute'
    session_fee = 'session_fee'
    parking_fee = 'parking_fee'

# Модели
class User(Base):
    __tablename__ = 'users'
    
    id = Column(String, primary_key=True)
    email = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(SqlEnum(UserRole), nullable=False)
    is_active = Column(Boolean, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    admin_id = Column(String, nullable=True)
    
    # Relationships
    charging_sessions = relationship("ChargingSession", back_populates="user")

class Client(Base):
    __tablename__ = 'clients'
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    contract_number = Column(String, nullable=True)
    contract_start_date = Column(DateTime(timezone=True), nullable=True)
    contract_end_date = Column(DateTime(timezone=True), nullable=True)
    status = Column(SqlEnum(ClientStatus), nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

class Location(Base):
    __tablename__ = 'locations'
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    address = Column(String, nullable=False)
    city = Column(String, nullable=True)
    country = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    client_id = Column(String, nullable=True)
    stations_count = Column(Integer, default=0)
    connectors_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    stations = relationship("Station", back_populates="location")

class TariffPlan(Base):
    __tablename__ = 'tariff_plans'
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    tariff_rules = relationship("TariffRule", back_populates="tariff_plan")
    stations = relationship("Station", back_populates="tariff_plan")

class TariffRule(Base):
    __tablename__ = 'tariff_rules'
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tariff_plan_id = Column(String, ForeignKey('tariff_plans.id'), nullable=True)
    name = Column(String, nullable=False)
    tariff_type = Column(SqlEnum(TariffType), default=TariffType.per_kwh, nullable=False)
    connector_type = Column(String, default='ALL')
    power_range_min = Column(Numeric, default=0)
    power_range_max = Column(Numeric, default=1000)
    price = Column(Numeric, nullable=False)
    currency = Column(String, default='KGS')
    time_start = Column(Text, default='00:00:00')
    time_end = Column(Text, default='23:59:59')
    is_weekend = Column(Boolean, default=False)
    priority = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    tariff_plan = relationship("TariffPlan", back_populates="tariff_rules")

class Station(Base):
    __tablename__ = 'stations'
    
    id = Column(String, primary_key=True)
    serial_number = Column(String, nullable=False, unique=True)
    model = Column(String, nullable=False)
    manufacturer = Column(String, nullable=False)
    location_id = Column(String, ForeignKey('locations.id'), nullable=False)
    power_capacity = Column(Float, nullable=False)
    connector_types = Column(ARRAY(String), nullable=False)  # Правильный тип для PostgreSQL массива
    installation_date = Column(String, nullable=True)
    firmware_version = Column(String, nullable=True)
    status = Column(SqlEnum(StationStatus), nullable=False)
    admin_id = Column(String, nullable=False)
    connectors_count = Column(Integer, default=1)
    tariff_plan_id = Column(String, ForeignKey('tariff_plans.id'), nullable=True)
    price_per_kwh = Column(Numeric, default=0)
    session_fee = Column(Numeric, default=0)
    currency = Column(String, default='KGS')
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    location = relationship("Location", back_populates="stations")
    tariff_plan = relationship("TariffPlan", back_populates="stations")
    charging_sessions = relationship("ChargingSession", back_populates="station")
    maintenance_records = relationship("Maintenance", back_populates="station")
    ocpp_status = relationship("OCPPStationStatus", back_populates="station")

class Maintenance(Base):
    __tablename__ = 'maintenance'
    
    id = Column(String, primary_key=True)
    station_id = Column(String, ForeignKey('stations.id'), nullable=False)
    request_date = Column(String, nullable=True)
    description = Column(String, nullable=True)
    assigned_to = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    status = Column(SqlEnum(MaintenanceStatus), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    station = relationship("Station", back_populates="maintenance_records")

class ChargingSession(Base):
    __tablename__ = 'charging_sessions'
    
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey('users.id'), nullable=False)
    station_id = Column(String, ForeignKey('stations.id'), nullable=False)
    start_time = Column(DateTime(timezone=True), server_default=func.now())
    stop_time = Column(DateTime(timezone=True), nullable=True)
    energy = Column(Float, nullable=True)  # kWh
    amount = Column(Float, nullable=True)  # стоимость
    status = Column(SqlEnum(ChargingSessionStatus), default=ChargingSessionStatus.started, nullable=False)
    transaction_id = Column(String, nullable=True)  # OCPP transaction id
    limit_type = Column(SqlEnum(LimitType), default=LimitType.none, nullable=False)
    limit_value = Column(Float, nullable=True)  # значение лимита
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    user = relationship("User", back_populates="charging_sessions")
    station = relationship("Station", back_populates="charging_sessions")

class OCPPStationStatus(Base):
    """OCPP Station Status - отслеживание состояния станций в реальном времени"""
    __tablename__ = "ocpp_station_status"

    station_id = Column(String, ForeignKey("stations.id", ondelete="CASCADE"), primary_key=True)
    status = Column(String, nullable=False, default="Available")  # Available, Preparing, Charging, etc.
    error_code = Column(String)  # NoError, ConnectorLockFailure, etc.
    info = Column(String)
    vendor_id = Column(String)
    vendor_error_code = Column(String)
    last_heartbeat = Column(DateTime(timezone=True), server_default=func.now())
    firmware_version = Column(String)
    boot_notification_sent = Column(Boolean, default=False)
    is_online = Column(Boolean, default=False)
    connector_status = Column(JSON, default=[])  # Array of connector statuses
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship
    station = relationship("Station", back_populates="ocpp_status")

class OCPPTransaction(Base):
    """OCPP Transactions - отслеживание OCPP транзакций"""
    __tablename__ = "ocpp_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_id = Column(Integer, nullable=False)
    station_id = Column(String, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    connector_id = Column(Integer, nullable=False, default=1)
    id_tag = Column(String, nullable=False)  # RFID/NFC tag
    meter_start = Column(Numeric, nullable=False, default=0)
    meter_stop = Column(Numeric)
    start_timestamp = Column(DateTime(timezone=True), nullable=False)
    stop_timestamp = Column(DateTime(timezone=True))
    stop_reason = Column(String)  # EmergencyStop, EVDisconnected, etc.
    charging_session_id = Column(String, ForeignKey("charging_sessions.id"))
    status = Column(String, nullable=False, default="Started")  # Started, Stopped
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    station = relationship("Station")
    charging_session = relationship("ChargingSession")
    meter_values = relationship("OCPPMeterValue", back_populates="transaction")

class OCPPMeterValue(Base):
    """OCPP Meter Values - показания счетчиков"""
    __tablename__ = "ocpp_meter_values"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_id = Column(Integer, nullable=True)  # OCPP transaction_id (not FK)
    ocpp_transaction_id = Column(Integer, ForeignKey("ocpp_transactions.id"), nullable=True)  # FK to OCPPTransaction.id
    station_id = Column(String, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    connector_id = Column(Integer, nullable=False, default=1)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    sampled_values = Column(JSON, nullable=False)  # Raw OCPP data
    energy_active_import_register = Column(Numeric)  # kWh delivered
    power_active_import = Column(Numeric)  # W current power
    current_import = Column(Numeric)  # A current
    voltage = Column(Numeric)  # V voltage
    temperature = Column(Numeric)  # °C temperature
    soc = Column(Numeric)  # % State of Charge
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    station = relationship("Station")
    transaction = relationship("OCPPTransaction", back_populates="meter_values", 
                             foreign_keys=[ocpp_transaction_id])

class OCPPAuthorization(Base):
    """OCPP Authorization - управление RFID/NFC тегами"""
    __tablename__ = "ocpp_authorization"

    id_tag = Column(String, primary_key=True)
    parent_id_tag = Column(String)
    expiry_date = Column(DateTime(timezone=True))
    status = Column(String, nullable=False, default="Accepted")  # Accepted, Blocked, Expired, etc.
    user_id = Column(String, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User")

class OCPPConfiguration(Base):
    """OCPP Configuration - конфигурация станций"""
    __tablename__ = "ocpp_configuration"

    id = Column(Integer, primary_key=True, autoincrement=True)
    station_id = Column(String, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(String)
    readonly = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    station = relationship("Station")

# ============================================================================
# ПЛАТЕЖНАЯ СИСТЕМА O!DENGI
# ============================================================================

class PaymentStatus(str, enum.Enum):
    pending = "pending"           # 0 - Ожидает оплаты
    paid = "paid"                # 1 - Оплачено  
    cancelled = "cancelled"      # 2 - Отменено
    refunded = "refunded"        # 3 - Возврат
    partial_refund = "partial_refund"  # 4 - Частичный возврат

class PaymentType(str, enum.Enum):
    balance_topup = "balance_topup"     # Пополнение баланса
    charging_payment = "charging_payment"  # Прямая оплата зарядки

class BalanceTopup(Base):
    """Пополнения баланса клиентов через O!Dengi"""
    __tablename__ = "balance_topups"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # O!Dengi данные
    invoice_id = Column(String(12), unique=True, index=True)
    order_id = Column(String(128), unique=True, index=True)
    merchant_id = Column(String(32))
    
    # Клиент и сумма
    client_id = Column(String, ForeignKey('clients.id'), nullable=False)
    requested_amount = Column(Numeric, nullable=False)  # Запрошенная сумма
    paid_amount = Column(Numeric, nullable=True)  # Фактически оплаченная сумма
    currency = Column(String(3), default="KGS")
    
    # Статусы и временные метки
    status = Column(SqlEnum(PaymentStatus), default=PaymentStatus.pending)
    odengi_status = Column(Integer, default=0)  # Статус от O!Dengi API
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Дополнительные данные
    description = Column(Text)
    qr_code_url = Column(String(500))
    app_link = Column(String(500))
    
    # Webhook данные
    last_webhook_at = Column(DateTime(timezone=True), nullable=True)
    webhook_count = Column(Integer, default=0)
    
    # Relationships
    client = relationship("Client")

class ChargingPayment(Base):
    """Прямые платежи за зарядку через O!Dengi"""
    __tablename__ = "charging_payments"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # O!Dengi данные
    invoice_id = Column(String(12), unique=True, index=True)
    order_id = Column(String(128), unique=True, index=True)
    merchant_id = Column(String(32))
    
    # ЭЗС данные
    station_id = Column(String, ForeignKey('stations.id'), nullable=False)
    connector_id = Column(Integer, nullable=False)
    client_id = Column(String, ForeignKey('clients.id'), nullable=False)
    charging_session_id = Column(String, ForeignKey('charging_sessions.id'), nullable=True)
    
    # Финансовые данные
    estimated_amount = Column(Numeric, nullable=False)  # Предварительная сумма
    paid_amount = Column(Numeric, nullable=True)  # Фактически оплачено
    currency = Column(String(3), default="KGS")
    
    # Зарядная сессия
    estimated_kwh = Column(Numeric)
    actual_kwh = Column(Numeric, nullable=True)
    rate_per_kwh = Column(Numeric)
    
    # Статусы и временные метки
    status = Column(SqlEnum(PaymentStatus), default=PaymentStatus.pending)
    odengi_status = Column(Integer, default=0)  # Статус от O!Dengi API
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Дополнительные данные
    description = Column(Text)
    qr_code_url = Column(String(500))
    app_link = Column(String(500))
    
    # Webhook данные
    last_webhook_at = Column(DateTime(timezone=True), nullable=True)
    webhook_count = Column(Integer, default=0)
    
    # Relationships
    station = relationship("Station")
    client = relationship("Client")
    charging_session = relationship("ChargingSession")

class PaymentTransaction(Base):
    """Лог всех операций с балансом и платежами"""
    __tablename__ = "payment_transactions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    client_id = Column(String, ForeignKey('clients.id'), nullable=False)
    transaction_type = Column(SqlEnum(PaymentType), nullable=False)
    
    # Суммы
    amount = Column(Numeric, nullable=False)
    balance_before = Column(Numeric, nullable=False)
    balance_after = Column(Numeric, nullable=False)
    currency = Column(String(3), default="KGS")
    
    # Связанные объекты
    balance_topup_id = Column(Integer, ForeignKey('balance_topups.id'), nullable=True)
    charging_payment_id = Column(Integer, ForeignKey('charging_payments.id'), nullable=True)
    charging_session_id = Column(String, ForeignKey('charging_sessions.id'), nullable=True)
    
    # Метаданные
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    client = relationship("Client")
    balance_topup = relationship("BalanceTopup")
    charging_payment = relationship("ChargingPayment")
    charging_session = relationship("ChargingSession")
