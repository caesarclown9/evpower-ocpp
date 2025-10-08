from typing import Optional, List
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime, time, date
from decimal import Decimal
# validator устарел в Pydantic v2, используем field_validator при необходимости

# Enums
class OCPPConnectionStatus(str, Enum):
    active = 'active'
    inactive = 'inactive'
    error = 'error'

class UserRole(str, Enum):
    operator = 'operator'
    admin = 'admin'
    superadmin = 'superadmin'

class ClientStatus(str, Enum):
    active = 'active'
    inactive = 'inactive'
    blocked = 'blocked'

class StationStatus(str, Enum):
    active = 'active'
    inactive = 'inactive'
    maintenance = 'maintenance'

class MaintenanceStatus(str, Enum):
    pending = 'pending'
    in_progress = 'in_progress'
    completed = 'completed'
    cancelled = 'cancelled'

class ChargingSessionStatus(str, Enum):
    started = 'started'
    stopped = 'stopped'
    error = 'error'

class LimitType(str, Enum):
    none = 'none'
    energy = 'energy'
    amount = 'amount'

class TariffType(str, Enum):
    per_kwh = 'per_kwh'
    per_minute = 'per_minute'
    session_fee = 'session_fee'
    parking_fee = 'parking_fee'

# OCPP Connection schemas (для WebSocket)
class OCPPConnectionBase(BaseModel):
    station_id: str
    status: OCPPConnectionStatus

class OCPPConnectionCreate(BaseModel):
    station_id: str

class OCPPConnection(OCPPConnectionBase):
    id: str
    last_heartbeat: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

class OCPPTransactionStatus(str, Enum):
    started = 'started'
    stopped = 'stopped'
    error = 'error'

class OCPPTransactionBase(BaseModel):
    connection_id: str
    energy: Optional[float] = None
    status: OCPPTransactionStatus

class OCPPTransactionCreate(BaseModel):
    connection_id: str

class OCPPTransaction(OCPPTransactionBase):
    id: str
    start_time: Optional[datetime] = None
    stop_time: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

# User schemas
class UserBase(BaseModel):
    email: str
    role: UserRole
    is_active: Optional[bool] = None
    admin_id: Optional[str] = None

class UserCreate(UserBase):
    password: str

class User(UserBase):
    id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Client schemas
class ClientBase(BaseModel):
    name: Optional[str] = None  # В БД nullable=True
    phone: Optional[str] = None
    balance: Optional[float] = 0.0  # ДОБАВЛЕНО: поле из БД
    status: ClientStatus

class ClientCreate(ClientBase):
    pass  # Убираем password - его нет в БД

class Client(ClientBase):
    id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Location schemas
class LocationBase(BaseModel):
    name: str
    address: str
    city: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    user_id: str  # ИСПРАВЛЕНО: user_id вместо client_id (сотрудники управляют локациями)

class LocationCreate(LocationBase):
    pass

class Location(LocationBase):
    id: str
    stations_count: int = 0
    connectors_count: int = 0
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

# TariffPlan schemas
class TariffPlanBase(BaseModel):
    name: str
    description: Optional[str] = None
    is_default: bool = False
    is_active: bool = True

class TariffPlanCreate(TariffPlanBase):
    pass

class TariffPlan(TariffPlanBase):
    id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

# TariffRule schemas
class TariffRuleBase(BaseModel):
    tariff_plan_id: Optional[str] = None
    name: str
    tariff_type: TariffType = TariffType.per_kwh
    connector_type: str = 'ALL'
    power_range_min: Decimal = Decimal('0')
    power_range_max: Decimal = Decimal('1000')
    price: Decimal
    currency: str = 'KGS'
    time_start: time = time(0, 0, 0)
    time_end: time = time(23, 59, 59)
    is_weekend: bool = False
    priority: int = 0
    is_active: bool = True

class TariffRuleCreate(TariffRuleBase):
    pass

class TariffRule(TariffRuleBase):
    id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Station schemas
class StationBase(BaseModel):
    serial_number: str
    model: str
    manufacturer: str
    location_id: str
    power_capacity: float
    connector_types: List[str]
    installation_date: Optional[str] = None
    firmware_version: Optional[str] = None
    status: StationStatus
    user_id: str  # ИСПРАВЛЕНО: user_id вместо admin_id (сотрудники владеют станциями)
    connectors_count: int = 1
    tariff_plan_id: Optional[str] = None
    price_per_kwh: Decimal = Decimal('0')
    session_fee: Decimal = Decimal('0')
    currency: str = 'KGS'

class StationCreate(StationBase):
    pass

class Station(StationBase):
    id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Maintenance schemas
class MaintenanceBase(BaseModel):
    station_id: str
    request_date: Optional[str] = None
    description: Optional[str] = None
    assigned_to: Optional[str] = None
    notes: Optional[str] = None
    status: MaintenanceStatus

class MaintenanceCreate(MaintenanceBase):
    pass

class Maintenance(MaintenanceBase):
    id: str
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

# ChargingSession schemas
class ChargingSessionBase(BaseModel):
    user_id: str
    station_id: str
    limit_type: LimitType = LimitType.none
    limit_value: Optional[float] = None

class ChargingSessionCreate(ChargingSessionBase):
    pass

class ChargingSession(ChargingSessionBase):
    id: str
    start_time: datetime
    stop_time: Optional[datetime] = None
    energy: Optional[float] = None
    amount: Optional[float] = None
    status: ChargingSessionStatus
    transaction_id: Optional[str] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# ============================================================================
# СХЕМЫ ДЛЯ ПЛАТЕЖНОЙ СИСТЕМЫ O!DENGI
# ============================================================================

class PaymentStatus(str, Enum):
    processing = "processing"
    approved = "approved"
    canceled = "canceled"
    refunded = "refunded"
    partial_refund = "partial_refund"

class PaymentType(str, Enum):
    balance_topup = "balance_topup"      # Пополнение баланса
    charge_reserve = "charge_reserve"    # Резерв при зарядке
    charge_refund = "charge_refund"      # Возврат после зарядки  
    charge_payment = "charge_payment"    # Доплата за превышение резерва

# ===== REQUEST SCHEMAS =====

class BalanceTopupRequest(BaseModel):
    """Запрос на пополнение баланса"""
    amount: float = Field(..., gt=0, le=100000, description="Сумма пополнения в сомах")
    description: Optional[str] = Field(None, description="Описание платежа")

class H2HPaymentRequest(BaseModel):
    """Запрос на H2H платеж картой"""
    amount: float = Field(..., gt=0, le=100000, description="Сумма платежа в сомах")
    card_pan: str = Field(..., min_length=12, max_length=19, description="Номер карты")
    card_name: str = Field(..., min_length=1, max_length=100, description="Имя владельца карты")
    card_cvv: str = Field(..., min_length=3, max_length=4, description="CVV код")
    card_year: str = Field(..., pattern=r"^\d{2}$", description="Год истечения карты (YY)")
    card_month: str = Field(..., pattern=r"^(0[1-9]|1[0-2])$", description="Месяц истечения карты (MM)")
    email: str = Field(..., description="Email клиента")
    phone_number: Optional[str] = Field(None, description="Номер телефона клиента")
    description: Optional[str] = Field(None, description="Описание платежа")

class TokenPaymentRequest(BaseModel):
    """Запрос на платеж по токену карты"""
    amount: float = Field(..., gt=0, le=100000, description="Сумма платежа в сомах")
    card_token: str = Field(..., min_length=1, description="Токен сохраненной карты")
    email: str = Field(..., description="Email клиента")
    description: Optional[str] = Field(None, description="Описание платежа")

class CreateTokenRequest(BaseModel):
    """Запрос на создание токена для сохранения карт"""
    days: int = Field(default=14, ge=1, le=14, description="Количество дней действия токена")

class PaymentWebhookData(BaseModel):
    """Webhook данные от O!Dengi"""
    merchant_id: str
    invoice_id: str
    order_id: str
    status: int
    amount: Optional[int] = None
    currency: Optional[str] = "KGS"
    paid_amount: Optional[int] = None
    commission: Optional[int] = None
    transaction_id: Optional[str] = None
    timestamp: Optional[str] = None
    user_phone: Optional[str] = None
    description: Optional[str] = None

# ===== RESPONSE SCHEMAS =====

class BalanceTopupResponse(BaseModel):
    """Ответ на запрос пополнения баланса"""
    success: bool
    invoice_id: Optional[str] = None
    order_id: Optional[str] = None
    qr_code: Optional[str] = None  # Данные QR-кода для генерации в приложении
    qr_code_url: Optional[str] = None  # URL картинки QR-кода (для веб)
    app_link: Optional[str] = None
    amount: Optional[float] = None
    client_id: str
    current_balance: Optional[float] = None
    
    # 🕐 Время жизни платежа
    qr_expires_at: Optional[datetime] = None  # Когда истекает QR код
    invoice_expires_at: Optional[datetime] = None  # Когда истекает invoice
    qr_lifetime_seconds: int = 300  # 5 минут для QR
    invoice_lifetime_seconds: int = 600  # 10 минут для invoice
    
    error: Optional[str] = None

class PaymentStatusResponse(BaseModel):
    """Статус платежа"""
    success: bool
    status: int
    status_text: str
    amount: Optional[float] = None
    paid_amount: Optional[float] = None
    invoice_id: Optional[str] = None
    can_proceed: bool = False  # Для пополнения - можно ли зачислить на баланс
    can_start_charging: bool = False  # Для зарядки - можно ли начать зарядку
    
    # 🕐 Время жизни и проверки
    qr_expired: bool = False  # QR код истек
    invoice_expired: bool = False  # Invoice истек
    qr_expires_at: Optional[datetime] = None
    invoice_expires_at: Optional[datetime] = None
    last_status_check_at: Optional[datetime] = None
    needs_callback_check: bool = False  # Требуется callback проверка
    
    error: Optional[str] = None

class ClientBalanceInfo(BaseModel):
    """Информация о балансе клиента"""
    client_id: str
    balance: float
    currency: str = "KGS"
    last_topup_at: Optional[datetime] = None
    total_spent: Optional[float] = None

class PaymentTransactionInfo(BaseModel):
    """Информация о транзакции"""
    id: int
    client_id: str
    transaction_type: PaymentType
    amount: float
    balance_before: float
    balance_after: float
    description: Optional[str] = None
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

class BalanceTopupInfo(BaseModel):
    """Информация о пополнении баланса"""
    id: int
    invoice_id: str
    order_id: str
    client_id: str
    requested_amount: float
    paid_amount: Optional[float] = None
    status: PaymentStatus
    created_at: datetime
    paid_at: Optional[datetime] = None
    qr_code_url: Optional[str] = None
    app_link: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)

class ChargingPaymentInfo(BaseModel):
    """Информация о платеже за зарядку"""
    id: int
    invoice_id: str
    order_id: str
    station_id: str
    connector_id: int
    client_id: str
    estimated_amount: float
    paid_amount: Optional[float] = None
    status: PaymentStatus
    estimated_kwh: float
    rate_per_kwh: float
    created_at: datetime
    paid_at: Optional[datetime] = None
    qr_code_url: Optional[str] = None
    app_link: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)

class H2HPaymentResponse(BaseModel):
    """Ответ на H2H платеж"""
    success: bool
    transaction_id: Optional[str] = None
    auth_key: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    client_id: str
    current_balance: Optional[float] = None
    error: Optional[str] = None

class TokenPaymentResponse(BaseModel):
    """Ответ на токен-платеж"""
    success: bool
    transaction_id: Optional[str] = None
    auth_key: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    client_id: str
    current_balance: Optional[float] = None
    error: Optional[str] = None

class CreateTokenResponse(BaseModel):
    """Ответ на создание токена"""
    success: bool
    token_url: Optional[str] = None
    token_expires_in_days: Optional[int] = None
    message: Optional[str] = None
    error: Optional[str] = None
