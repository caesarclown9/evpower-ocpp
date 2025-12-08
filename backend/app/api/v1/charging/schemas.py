"""
Pydantic schemas для Charging API
"""
from pydantic import BaseModel, Field, validator, constr
from typing import Optional
import re

class ChargingStartRequest(BaseModel):
    """🔌 Запрос на начало зарядки"""
    # Строгая валидация station_id: только буквы, цифры, дефис
    # Примеры: CHR-BGK-001, STN-OSH-042
    station_id: constr(min_length=1, max_length=50, pattern=r'^[A-Z0-9\-]+$') = Field(
        ...,
        description="ID станции (формат: CHR-BGK-001)"
    )
    connector_id: int = Field(..., ge=1, le=10, description="Номер коннектора (1-10)")
    energy_kwh: Optional[float] = Field(None, gt=0, le=200, description="Энергия для зарядки в кВт⋅ч")
    amount_som: Optional[float] = Field(None, gt=0, le=100000, description="Предоплаченная сумма в сомах")

    @validator('station_id')
    def validate_station_id(cls, v):
        """Валидация формата station_id для защиты от SQL injection"""
        if not re.match(r'^[A-Z0-9\-]+$', v):
            raise ValueError('Invalid station_id format')
        return v

    @validator('amount_som', 'energy_kwh')
    def validate_limits(cls, v, values):
        """Валидация лимитов зарядки"""
        # Поддерживаем 3 режима:
        # 1. energy_kwh + amount_som - лимит по энергии с максимальной суммой
        # 2. Только amount_som - лимит по сумме
        # 3. Ничего не указано - полностью безлимитная зарядка
        return v

class ChargingStopRequest(BaseModel):
    """⏹️ Запрос на остановку зарядки"""
    # UUID формат для session_id
    session_id: constr(
        min_length=1,
        max_length=100,
        pattern=r'^[a-zA-Z0-9\-]+$'
    ) = Field(..., description="ID сессии зарядки (UUID или alphanumeric)")

    @validator('session_id')
    def validate_session_id(cls, v):
        """Валидация формата session_id для защиты от SQL injection"""
        if not re.match(r'^[a-zA-Z0-9\-]+$', v):
            raise ValueError('Invalid session_id format')
        return v

class ChargingStopResponse(BaseModel):
    """⏹️ Ответ об остановке зарядки"""
    success: bool
    session_id: Optional[str] = None
    station_id: Optional[str] = None
    client_id: Optional[str] = None
    start_time: Optional[str] = None
    stop_time: Optional[str] = None
    energy_consumed: Optional[float] = None
    rate_per_kwh: Optional[float] = None
    reserved_amount: Optional[float] = None
    actual_cost: Optional[float] = None
    refund_amount: Optional[float] = None
    new_balance: Optional[float] = None
    station_online: Optional[bool] = None
    error: Optional[str] = None
    message: Optional[str] = None

class ChargingSessionData(BaseModel):
    """📊 Данные сессии зарядки (вложенный объект)"""
    id: str
    session_id: Optional[str] = None  # дублирует id для совместимости
    status: str
    station_id: str
    connector_id: Optional[int] = None
    start_time: Optional[str] = None
    stop_time: Optional[str] = None

    # Энергетические данные
    energy_consumed: Optional[float] = 0
    energy_kwh: Optional[float] = 0  # дублирует energy_consumed
    current_cost: Optional[float] = 0
    current_amount: Optional[float] = 0  # дублирует current_cost
    power_kw: Optional[float] = 0

    # Длительность
    charging_duration_minutes: Optional[int] = 0
    duration_seconds: Optional[int] = 0

    # Резерв и тарифы
    reserved_amount: Optional[float] = 0
    rate_per_kwh: Optional[float] = 0
    session_fee: Optional[float] = 0

    # Лимиты и прогресс
    limit_type: Optional[str] = "none"
    limit_value: Optional[float] = 0
    limit_reached: Optional[bool] = False
    limit_percentage: Optional[float] = 0
    progress_percent: Optional[float] = 0  # дублирует limit_percentage

    # OCPP данные
    ocpp_transaction_id: Optional[int] = None
    meter_start: Optional[float] = 0
    meter_current: Optional[float] = 0

    # Данные EV
    ev_battery_soc: Optional[int] = None

    # Статус станции
    station_online: Optional[bool] = True

    class Config:
        extra = "allow"  # Разрешаем дополнительные поля для совместимости


class ChargingStatusResponse(BaseModel):
    """📊 Ответ о статусе зарядки (формат Voltera с вложенным session)"""
    success: bool
    session: Optional[ChargingSessionData] = None
    error: Optional[str] = None
    message: Optional[str] = None

    class Config:
        extra = "allow"  # Разрешаем дополнительные поля для совместимости