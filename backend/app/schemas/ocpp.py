from typing import Optional, List
from enum import Enum
from pydantic import BaseModel, ConfigDict
from datetime import datetime

class OCPPConnectionStatus(str, Enum):
    active = 'active'
    inactive = 'inactive'
    error = 'error'

class OCPPConnectionBase(BaseModel):
    station_id: str
    status: OCPPConnectionStatus

class OCPPConnectionCreate(BaseModel):
    station_id: str

class OCPPConnection(OCPPConnectionBase):
    id: str
    last_heartbeat: Optional[datetime] = None  # Только для вывода
    model_config = ConfigDict(from_attributes=True)

class OCPPTransactionStatus(str, Enum):
    started = 'started'
    stopped = 'stopped'
    error = 'error'

class OCPPTransactionBase(BaseModel):
    connection_id: str
    energy: Optional[float] = None  # kWh
    status: OCPPTransactionStatus

class OCPPTransactionCreate(BaseModel):
    connection_id: str

class OCPPTransaction(OCPPTransactionBase):
    id: str
    start_time: Optional[datetime] = None  # Только для вывода
    stop_time: Optional[datetime] = None  # Только для вывода
    model_config = ConfigDict(from_attributes=True)

# --- Новые схемы для тарифов и сессий зарядки ---
class LimitType(str, Enum):
    none = 'none'
    energy = 'energy'
    amount = 'amount'

class ChargingSessionStatus(str, Enum):
    started = 'started'
    stopped = 'stopped'
    error = 'error'

class TariffBase(BaseModel):
    station_id: str
    price_per_kwh: float
    currency: str = 'KGS'

class TariffCreate(TariffBase):
    pass

class Tariff(TariffBase):
    id: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ChargingSessionBase(BaseModel):
    user_id: str
    station_id: str
    limit_type: LimitType = LimitType.none
    limit_value: float | None = None

class ChargingSessionCreate(ChargingSessionBase):
    pass

class ChargingSession(ChargingSessionBase):
    id: str
    start_time: datetime
    stop_time: datetime | None = None
    energy: float | None = None
    amount: float | None = None
    status: ChargingSessionStatus
    transaction_id: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
