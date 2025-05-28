from typing import Optional, List
from enum import Enum
from pydantic import BaseModel, ConfigDict
from datetime import datetime, time, date
from decimal import Decimal

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
    name: str
    email: str
    phone: Optional[str] = None
    address: Optional[str] = None
    contract_number: Optional[str] = None
    contract_start_date: Optional[datetime] = None
    contract_end_date: Optional[datetime] = None
    status: ClientStatus

class ClientCreate(ClientBase):
    password: str

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
    client_id: Optional[str] = None

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
    admin_id: str
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
