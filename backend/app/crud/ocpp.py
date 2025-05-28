from sqlalchemy.orm import Session
from sqlalchemy import select, update, delete
from typing import Optional, List
from app.db.models.ocpp import (
    User, Client, Location, Station, Maintenance, 
    ChargingSession, TariffPlan, TariffRule
)
from app.schemas.ocpp import (
    UserCreate, ClientCreate, LocationCreate, StationCreate, 
    MaintenanceCreate, ChargingSessionCreate, TariffPlanCreate, 
    TariffRuleCreate
)
from decimal import Decimal
import uuid

# --- User CRUD ---
def create_user(db: Session, user_in: UserCreate) -> User:
    user_data = user_in.model_dump()
    user_data['id'] = str(uuid.uuid4())
    # В реальной системе нужно хэшировать пароль
    user_data['hashed_password'] = user_data.pop('password')
    user = User(**user_data)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

def get_user(db: Session, user_id: str) -> Optional[User]:
    result = db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    result = db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()

def list_users(db: Session) -> List[User]:
    result = db.execute(select(User))
    return result.scalars().all()

# --- Client CRUD ---
def create_client(db: Session, client_in: ClientCreate) -> Client:
    client_data = client_in.model_dump()
    client_data['id'] = str(uuid.uuid4())
    client_data['hashed_password'] = client_data.pop('password')
    client = Client(**client_data)
    db.add(client)
    db.commit()
    db.refresh(client)
    return client

def get_client(db: Session, client_id: str) -> Optional[Client]:
    result = db.execute(select(Client).where(Client.id == client_id))
    return result.scalar_one_or_none()

def list_clients(db: Session) -> List[Client]:
    result = db.execute(select(Client))
    return result.scalars().all()

# --- Location CRUD ---
def create_location(db: Session, location_in: LocationCreate) -> Location:
    location_data = location_in.model_dump()
    location_data['id'] = str(uuid.uuid4())
    location = Location(**location_data)
    db.add(location)
    db.commit()
    db.refresh(location)
    return location

def get_location(db: Session, location_id: str) -> Optional[Location]:
    result = db.execute(select(Location).where(Location.id == location_id))
    return result.scalar_one_or_none()

def list_locations(db: Session) -> List[Location]:
    result = db.execute(select(Location))
    return result.scalars().all()

# --- TariffPlan CRUD ---
def create_tariff_plan(db: Session, tariff_plan_in: TariffPlanCreate) -> TariffPlan:
    tariff_plan = TariffPlan(**tariff_plan_in.model_dump())
    db.add(tariff_plan)
    db.commit()
    db.refresh(tariff_plan)
    return tariff_plan

def get_tariff_plan(db: Session, tariff_plan_id: str) -> Optional[TariffPlan]:
    result = db.execute(select(TariffPlan).where(TariffPlan.id == tariff_plan_id))
    return result.scalar_one_or_none()

def list_tariff_plans(db: Session, is_active: Optional[bool] = None) -> List[TariffPlan]:
    stmt = select(TariffPlan)
    if is_active is not None:
        stmt = stmt.where(TariffPlan.is_active == is_active)
    result = db.execute(stmt)
    return result.scalars().all()

def get_default_tariff_plan(db: Session) -> Optional[TariffPlan]:
    result = db.execute(select(TariffPlan).where(TariffPlan.is_default == True))
    return result.scalar_one_or_none()

# --- TariffRule CRUD ---
def create_tariff_rule(db: Session, tariff_rule_in: TariffRuleCreate) -> TariffRule:
    tariff_rule = TariffRule(**tariff_rule_in.model_dump())
    db.add(tariff_rule)
    db.commit()
    db.refresh(tariff_rule)
    return tariff_rule

def get_tariff_rule(db: Session, tariff_rule_id: str) -> Optional[TariffRule]:
    result = db.execute(select(TariffRule).where(TariffRule.id == tariff_rule_id))
    return result.scalar_one_or_none()

def list_tariff_rules(db: Session, tariff_plan_id: Optional[str] = None) -> List[TariffRule]:
    stmt = select(TariffRule)
    if tariff_plan_id:
        stmt = stmt.where(TariffRule.tariff_plan_id == tariff_plan_id)
    result = db.execute(stmt)
    return result.scalars().all()

# --- Station CRUD ---
def create_station(db: Session, station_in: StationCreate) -> Station:
    station_data = station_in.model_dump()
    station_data['id'] = str(uuid.uuid4())
    station = Station(**station_data)
    db.add(station)
    db.commit()
    db.refresh(station)
    return station

def get_station(db: Session, station_id: str) -> Optional[Station]:
    result = db.execute(select(Station).where(Station.id == station_id))
    return result.scalar_one_or_none()

def get_station_by_serial(db: Session, serial_number: str) -> Optional[Station]:
    result = db.execute(select(Station).where(Station.serial_number == serial_number))
    return result.scalar_one_or_none()

def list_stations(db: Session, location_id: Optional[str] = None) -> List[Station]:
    stmt = select(Station)
    if location_id:
        stmt = stmt.where(Station.location_id == location_id)
    result = db.execute(stmt)
    return result.scalars().all()

def update_station(db: Session, station_id: str, data: dict) -> Optional[Station]:
    db.execute(update(Station).where(Station.id == station_id).values(**data))
    db.commit()
    return get_station(db, station_id)

# --- Maintenance CRUD ---
def create_maintenance(db: Session, maintenance_in: MaintenanceCreate) -> Maintenance:
    maintenance_data = maintenance_in.model_dump()
    maintenance_data['id'] = str(uuid.uuid4())
    maintenance = Maintenance(**maintenance_data)
    db.add(maintenance)
    db.commit()
    db.refresh(maintenance)
    return maintenance

def get_maintenance(db: Session, maintenance_id: str) -> Optional[Maintenance]:
    result = db.execute(select(Maintenance).where(Maintenance.id == maintenance_id))
    return result.scalar_one_or_none()

def list_maintenance(db: Session, station_id: Optional[str] = None) -> List[Maintenance]:
    stmt = select(Maintenance)
    if station_id:
        stmt = stmt.where(Maintenance.station_id == station_id)
    result = db.execute(stmt)
    return result.scalars().all()

# --- ChargingSession CRUD ---
def create_charging_session(db: Session, session_in: ChargingSessionCreate) -> ChargingSession:
    session_data = session_in.model_dump()
    session_data['id'] = str(uuid.uuid4())
    session = ChargingSession(**session_data)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session

def get_charging_session(db: Session, session_id: str) -> Optional[ChargingSession]:
    result = db.execute(select(ChargingSession).where(ChargingSession.id == session_id))
    return result.scalar_one_or_none()

def get_charging_session_by_transaction(db: Session, transaction_id: str) -> Optional[ChargingSession]:
    result = db.execute(select(ChargingSession).where(ChargingSession.transaction_id == transaction_id))
    return result.scalar_one_or_none()

def list_charging_sessions(db: Session, user_id: Optional[str] = None, station_id: Optional[str] = None) -> List[ChargingSession]:
    stmt = select(ChargingSession)
    if user_id:
        stmt = stmt.where(ChargingSession.user_id == user_id)
    if station_id:
        stmt = stmt.where(ChargingSession.station_id == station_id)
    result = db.execute(stmt)
    return result.scalars().all()

def update_charging_session(db: Session, session_id: str, data: dict) -> Optional[ChargingSession]:
    db.execute(update(ChargingSession).where(ChargingSession.id == session_id).values(**data))
    db.commit()
    return get_charging_session(db, session_id)

def delete_charging_session(db: Session, session_id: str) -> None:
    db.execute(delete(ChargingSession).where(ChargingSession.id == session_id))
    db.commit()

# --- Функции для расчета тарифов ---
def calculate_charging_cost(db: Session, station_id: str, energy_kwh: float) -> dict:
    """Рассчитывает стоимость зарядки на основе тарифных планов"""
    station = get_station(db, station_id)
    if not station:
        return {"cost": 0, "currency": "KGS", "error": "Station not found"}
    
    # Если у станции есть прямые настройки тарифа
    if station.price_per_kwh and station.price_per_kwh > 0:
        cost = float(station.price_per_kwh) * energy_kwh + float(station.session_fee or 0)
        return {
            "cost": round(cost, 2),
            "currency": station.currency,
            "price_per_kwh": float(station.price_per_kwh),
            "session_fee": float(station.session_fee or 0)
        }
    
    # Используем тарифный план
    if station.tariff_plan_id:
        tariff_rules = list_tariff_rules(db, station.tariff_plan_id)
        # Здесь можно добавить логику для сложных тарифных правил
        # Пока используем простейший тариф
        for rule in tariff_rules:
            if rule.tariff_type.value == 'per_kwh':
                cost = float(rule.price) * energy_kwh
                return {
                    "cost": round(cost, 2),
                    "currency": rule.currency,
                    "price_per_kwh": float(rule.price),
                    "rule_name": rule.name
                }
    
    # Дефолтный тариф
    default_plan = get_default_tariff_plan(db)
    if default_plan:
        rules = list_tariff_rules(db, default_plan.id)
        for rule in rules:
            if rule.tariff_type.value == 'per_kwh':
                cost = float(rule.price) * energy_kwh
                return {
                    "cost": round(cost, 2),
                    "currency": rule.currency,
                    "price_per_kwh": float(rule.price),
                    "rule_name": rule.name
                }
    
    return {"cost": 0, "currency": "KGS", "error": "No tariff found"}

