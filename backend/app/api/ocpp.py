from fastapi import APIRouter, Depends, status, Body, HTTPException, Query
from typing import List, Optional
from app.schemas.ocpp import (
    OCPPConnection, OCPPConnectionCreate,
    OCPPTransaction, OCPPTransactionCreate,
    ChargingSession, ChargingSessionCreate, LimitType, ChargingSessionStatus,
    User, UserCreate, Station, StationCreate, Location, LocationCreate,
    TariffPlan, TariffPlanCreate, TariffRule, TariffRuleCreate
)
from app.db.session import get_db
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ocpp_ws_server.redis_manager import redis_manager
from app.crud.ocpp import (
    create_charging_session, get_charging_session, list_charging_sessions, update_charging_session, delete_charging_session,
    create_user, get_user, list_users, get_user_by_email,
    create_station, get_station, list_stations, get_station_by_serial, update_station,
    create_location, get_location, list_locations,
    create_tariff_plan, get_tariff_plan, list_tariff_plans,
    create_tariff_rule, get_tariff_rule, list_tariff_rules, calculate_charging_cost
)

router = APIRouter(prefix="/ocpp", tags=["ocpp"])

# --- Pydantic схемы для Swagger ---
class OCPPConnectionStatus(str):
    active = 'active'
    inactive = 'inactive'
    error = 'error'

class OCPPConnection(BaseModel):
    station_id: str = Field(..., example="DE-BERLIN-001")
    status: str = Field(..., example="active")
    last_heartbeat: Optional[str] = Field(None, example="2024-06-01T12:00:00Z")

class OCPPCommandRequest(BaseModel):
    station_id: str = Field(..., example="DE-BERLIN-001")
    command: str = Field(..., example="RemoteStartTransaction")
    payload: Optional[dict] = Field(None, example={"connectorId": 1})

class OCPPCommandResponse(BaseModel):
    status: str = Field(..., example="sent")
    station_id: str = Field(..., example="DE-BERLIN-001")
    command: str = Field(..., example="RemoteStartTransaction")
    result: Optional[dict] = Field(None, example={"status": "Accepted"})

# --- OCPP WebSocket эндпоинты ---

@router.get("/connections", response_model=List[OCPPConnection], summary="Список подключённых станций")
async def list_ocpp_connections():
    station_ids = await redis_manager.get_stations()
    return [
        OCPPConnection(station_id=station_id, status="active", last_heartbeat=None)
        for station_id in station_ids
    ]

@router.post("/connections", response_model=OCPPConnection, status_code=status.HTTP_201_CREATED)
async def create_ocpp_connection(connection_in: OCPPConnectionCreate):
    raise HTTPException(status_code=501, detail="Создание соединения реализуется через WebSocket-клиент.")

@router.get("/transactions", summary="List Ocpp Transactions")
async def list_ocpp_transactions(station_id: Optional[str] = Query(None)):
    txs = await redis_manager.get_transactions(station_id)
    return txs

@router.post("/transactions", summary="Create Ocpp Transaction")
async def create_ocpp_transaction(transaction_in: OCPPTransactionCreate):
    # Публикуем команду StartTransaction через Redis
    await redis_manager.publish_command(transaction_in.connection_id, {
        "command": "RemoteStartTransaction",
        "payload": {
            "connectorId": 1  # Можно доработать передачу connectorId
        }
    })
    return {"status": "sent", "station_id": transaction_in.connection_id}

@router.post("/send_command", response_model=OCPPCommandResponse, summary="Отправить команду на станцию через OCPP")
async def send_command(request: OCPPCommandRequest):
    await redis_manager.publish_command(request.station_id, {
        "command": request.command,
        "payload": request.payload or {}
    })
    return OCPPCommandResponse(
        status="sent",
        station_id=request.station_id,
        command=request.command,
        result={"info": "Команда отправлена через Redis Pub/Sub. Ожидайте выполнения на станции."}
    )

@router.get("/status/{station_id}", response_model=OCPPConnection, summary="Статус конкретной станции")
async def get_station_status(station_id: str):
    station_ids = await redis_manager.get_stations()
    status = "active" if station_id in station_ids else "inactive"
    return OCPPConnection(station_id=station_id, status=status, last_heartbeat=None)

@router.post("/disconnect", summary="Отключить станцию от WebSocket")
async def disconnect_station(station_id: str = Body(..., example="DE-BERLIN-001")):
    # Публикуем команду на отключение станции через Redis
    await redis_manager.publish_command(station_id, {"command": "Disconnect"})
    return {"status": "disconnect_sent", "station_id": station_id}

# --- CRUD для пользователей ---
@router.post("/users", response_model=User)
def create_user_api(user_in: UserCreate, db: Session = Depends(get_db)):
    # Проверяем, что пользователь не существует
    existing_user = get_user_by_email(db, user_in.email)
    if existing_user:
        raise HTTPException(400, "User with this email already exists")
    return create_user(db, user_in)

@router.get("/users", response_model=List[User])
def list_users_api(db: Session = Depends(get_db)):
    return list_users(db)

@router.get("/users/{user_id}", response_model=User)
def get_user_api(user_id: str, db: Session = Depends(get_db)):
    user = get_user(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    return user

# --- CRUD для станций ---
@router.post("/stations", response_model=Station)
def create_station_api(station_in: StationCreate, db: Session = Depends(get_db)):
    # Проверяем, что станция с таким серийным номером не существует
    existing_station = get_station_by_serial(db, station_in.serial_number)
    if existing_station:
        raise HTTPException(400, "Station with this serial number already exists")
    return create_station(db, station_in)

@router.get("/stations", response_model=List[Station])
def list_stations_api(location_id: Optional[str] = None, db: Session = Depends(get_db)):
    return list_stations(db, location_id)

@router.get("/stations/{station_id}", response_model=Station)
def get_station_api(station_id: str, db: Session = Depends(get_db)):
    station = get_station(db, station_id)
    if not station:
        raise HTTPException(404, "Station not found")
    return station

@router.put("/stations/{station_id}", response_model=Station)
def update_station_api(station_id: str, data: dict, db: Session = Depends(get_db)):
    station = update_station(db, station_id, data)
    if not station:
        raise HTTPException(404, "Station not found")
    return station

# --- CRUD для локаций ---
@router.post("/locations", response_model=Location)
def create_location_api(location_in: LocationCreate, db: Session = Depends(get_db)):
    return create_location(db, location_in)

@router.get("/locations", response_model=List[Location])
def list_locations_api(db: Session = Depends(get_db)):
    return list_locations(db)

@router.get("/locations/{location_id}", response_model=Location)
def get_location_api(location_id: str, db: Session = Depends(get_db)):
    location = get_location(db, location_id)
    if not location:
        raise HTTPException(404, "Location not found")
    return location

# --- CRUD для тарифных планов ---
@router.post("/tariff_plans", response_model=TariffPlan)
def create_tariff_plan_api(tariff_plan_in: TariffPlanCreate, db: Session = Depends(get_db)):
    return create_tariff_plan(db, tariff_plan_in)

@router.get("/tariff_plans", response_model=List[TariffPlan])
def list_tariff_plans_api(is_active: Optional[bool] = None, db: Session = Depends(get_db)):
    return list_tariff_plans(db, is_active)

@router.get("/tariff_plans/{tariff_plan_id}", response_model=TariffPlan)
def get_tariff_plan_api(tariff_plan_id: str, db: Session = Depends(get_db)):
    tariff_plan = get_tariff_plan(db, tariff_plan_id)
    if not tariff_plan:
        raise HTTPException(404, "Tariff plan not found")
    return tariff_plan

# --- CRUD для тарифных правил ---
@router.post("/tariff_rules", response_model=TariffRule)
def create_tariff_rule_api(tariff_rule_in: TariffRuleCreate, db: Session = Depends(get_db)):
    return create_tariff_rule(db, tariff_rule_in)

@router.get("/tariff_rules", response_model=List[TariffRule])
def list_tariff_rules_api(tariff_plan_id: Optional[str] = None, db: Session = Depends(get_db)):
    return list_tariff_rules(db, tariff_plan_id)

@router.get("/tariff_rules/{tariff_rule_id}", response_model=TariffRule)
def get_tariff_rule_api(tariff_rule_id: str, db: Session = Depends(get_db)):
    tariff_rule = get_tariff_rule(db, tariff_rule_id)
    if not tariff_rule:
        raise HTTPException(404, "Tariff rule not found")
    return tariff_rule

# --- CRUD для сессий зарядки ---
@router.post("/sessions", response_model=ChargingSession)
def create_session_api(session_in: ChargingSessionCreate, db: Session = Depends(get_db)):
    return create_charging_session(db, session_in)

@router.get("/sessions", response_model=List[ChargingSession])
def list_sessions_api(user_id: Optional[str] = None, station_id: Optional[str] = None, db: Session = Depends(get_db)):
    return list_charging_sessions(db, user_id, station_id)

@router.get("/sessions/{session_id}", response_model=ChargingSession)
def get_session_api(session_id: str, db: Session = Depends(get_db)):
    session = get_charging_session(db, session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return session

@router.delete("/sessions/{session_id}")
def delete_session_api(session_id: str, db: Session = Depends(get_db)):
    delete_charging_session(db, session_id)
    return {"status": "deleted"}

# --- Расчет стоимости зарядки ---
@router.get("/calculate_cost/{station_id}")
def calculate_cost_api(station_id: str, energy_kwh: float, db: Session = Depends(get_db)):
    """Рассчитать стоимость зарядки для станции"""
    return calculate_charging_cost(db, station_id, energy_kwh)

# --- Упрощенный запуск зарядки ---
class StartChargeRequest(BaseModel):
    station_id: str
    limit_type: LimitType = LimitType.none
    limit_value: float | None = None

@router.post("/start_charge", response_model=ChargingSession)
def start_charge(
    req: StartChargeRequest,
    db: Session = Depends(get_db)
):
    # Упрощенная версия без проверки пользователя и баланса
    session_in = ChargingSessionCreate(
        user_id="system",  # Системный пользователь
        station_id=req.station_id,
        limit_type=req.limit_type,
        limit_value=req.limit_value
    )
    session = create_charging_session(db, session_in)
    return session
