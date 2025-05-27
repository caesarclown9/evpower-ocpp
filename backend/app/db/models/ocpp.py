from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Enum as SqlEnum
from sqlalchemy.sql import func
import enum
import uuid
from app.db.base_class import Base

class ChargingSessionStatus(str, enum.Enum):
    started = 'started'
    stopped = 'stopped'
    error = 'error'

class LimitType(str, enum.Enum):
    none = 'none'
    energy = 'energy'  # лимит по кВт*ч
    amount = 'amount'  # лимит по сумме

class Tariff(Base):
    __tablename__ = 'tariffs'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    station_id = Column(String, ForeignKey('stations.id'), nullable=False)
    price_per_kwh = Column(Float, nullable=False)
    currency = Column(String, default='KGS', nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ChargingSession(Base):
    __tablename__ = 'charging_sessions'
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey('users.id'), nullable=False)
    station_id = Column(String, ForeignKey('stations.id'), nullable=False)
    start_time = Column(DateTime(timezone=True), server_default=func.now())
    stop_time = Column(DateTime(timezone=True), nullable=True)
    energy = Column(Float, nullable=True)  # kWh
    amount = Column(Float, nullable=True)  # KGS
    status = Column(SqlEnum(ChargingSessionStatus), default=ChargingSessionStatus.started, nullable=False)
    transaction_id = Column(String, nullable=True)  # OCPP transaction id
    limit_type = Column(SqlEnum(LimitType), default=LimitType.none, nullable=False)
    limit_value = Column(Float, nullable=True)  # значение лимита (кВт*ч или сумма)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
