from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Enum as SqlEnum, Boolean, Integer, Text, Time, Numeric, ARRAY
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
    time_start = Column(Time, default='00:00:00')
    time_end = Column(Time, default='23:59:59')
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
