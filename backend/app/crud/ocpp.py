from sqlalchemy.orm import Session
from sqlalchemy import select, update, delete
from app.db.models.ocpp import Tariff, ChargingSession
from app.schemas.ocpp import TariffCreate, ChargingSessionCreate

# --- Tariff CRUD ---
def create_tariff(db: Session, tariff_in: TariffCreate) -> Tariff:
    tariff = Tariff(**tariff_in.model_dump())
    db.add(tariff)
    db.commit()
    db.refresh(tariff)
    return tariff

def get_tariff(db: Session, tariff_id: str) -> Tariff | None:
    result = db.execute(select(Tariff).where(Tariff.id == tariff_id))
    return result.scalar_one_or_none()

def list_tariffs(db: Session, station_id: str | None = None) -> list[Tariff]:
    stmt = select(Tariff)
    if station_id:
        stmt = stmt.where(Tariff.station_id == station_id)
    result = db.execute(stmt)
    return result.scalars().all()

def update_tariff(db: Session, tariff_id: str, data: dict) -> Tariff | None:
    db.execute(update(Tariff).where(Tariff.id == tariff_id).values(**data))
    db.commit()
    return get_tariff(db, tariff_id)

def delete_tariff(db: Session, tariff_id: str) -> None:
    db.execute(delete(Tariff).where(Tariff.id == tariff_id))
    db.commit()

# --- ChargingSession CRUD ---
def create_charging_session(db: Session, session_in: ChargingSessionCreate) -> ChargingSession:
    session = ChargingSession(**session_in.model_dump())
    db.add(session)
    db.commit()
    db.refresh(session)
    return session

def get_charging_session(db: Session, session_id: str) -> ChargingSession | None:
    result = db.execute(select(ChargingSession).where(ChargingSession.id == session_id))
    return result.scalar_one_or_none()

def list_charging_sessions(db: Session, user_id: str | None = None, station_id: str | None = None) -> list[ChargingSession]:
    stmt = select(ChargingSession)
    if user_id:
        stmt = stmt.where(ChargingSession.user_id == user_id)
    if station_id:
        stmt = stmt.where(ChargingSession.station_id == station_id)
    result = db.execute(stmt)
    return result.scalars().all()

def update_charging_session(db: Session, session_id: str, data: dict) -> ChargingSession | None:
    db.execute(update(ChargingSession).where(ChargingSession.id == session_id).values(**data))
    db.commit()
    return get_charging_session(db, session_id)

def delete_charging_session(db: Session, session_id: str) -> None:
    db.execute(delete(ChargingSession).where(ChargingSession.id == session_id))
    db.commit()

