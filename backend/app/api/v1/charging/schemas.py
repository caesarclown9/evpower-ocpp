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

class ChargingStatusResponse(BaseModel):
    """📊 Ответ о статусе зарядки"""
    success: bool
    session_id: Optional[str] = None
    status: Optional[str] = None
    station_id: Optional[str] = None
    client_id: Optional[str] = None
    connector_id: Optional[int] = None
    start_time: Optional[str] = None
    stop_time: Optional[str] = None
    duration_minutes: Optional[int] = None
    energy_consumed: Optional[float] = None
    energy_consumed_kwh: Optional[float] = None
    cost: Optional[float] = None
    final_amount_som: Optional[float] = None
    amount_charged_som: Optional[float] = None
    limit_value: Optional[float] = None
    progress_percent: Optional[float] = None
    charging_power: Optional[float] = None
    station_current: Optional[float] = None
    station_voltage: Optional[float] = None
    ev_battery_soc: Optional[int] = None
    ev_current: Optional[float] = None
    ev_voltage: Optional[float] = None
    temperatures: Optional[dict] = None
    meter_start: Optional[int] = None
    meter_current: Optional[int] = None
    station_online: Optional[bool] = None
    last_update: Optional[str] = None
    current_energy: Optional[float] = None
    current_amount: Optional[float] = None
    limit_type: Optional[str] = None
    transaction_id: Optional[str] = None
    ocpp_transaction_id: Optional[str] = None
    rate_per_kwh: Optional[float] = None
    ocpp_status: Optional[str] = None
    has_meter_data: Optional[bool] = None
    error: Optional[str] = None
    message: Optional[str] = None