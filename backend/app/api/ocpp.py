from fastapi import APIRouter, Depends, status, Body, HTTPException, Query
from typing import List, Optional
from app.schemas.ocpp import (
    OCPPConnection, OCPPConnectionCreate,
    OCPPTransaction, OCPPTransactionCreate,
    Tariff, TariffCreate, ChargingSession, ChargingSessionCreate, LimitType, ChargingSessionStatus
)
from app.db.session import get_db
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ocpp_ws_server.redis_manager import redis_manager
from app.crud.ocpp import (
    create_tariff, get_tariff, list_tariffs, update_tariff, delete_tariff,
    create_charging_session, get_charging_session, list_charging_sessions, update_charging_session, delete_charging_session
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

# --- Эндпоинты ---

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

# --- CRUD для тарифов ---
@router.post("/tariffs", response_model=Tariff)
def create_tariff_api(tariff_in: TariffCreate, db: Session = Depends(get_db)):
    return create_tariff(db, tariff_in)

@router.get("/tariffs", response_model=List[Tariff])
def list_tariffs_api(station_id: Optional[str] = None, db: Session = Depends(get_db)):
    return list_tariffs(db, station_id)

@router.get("/tariffs/{tariff_id}", response_model=Tariff)
def get_tariff_api(tariff_id: str, db: Session = Depends(get_db)):
    tariff = get_tariff(db, tariff_id)
    if not tariff:
        raise HTTPException(404, "Tariff not found")
    return tariff

@router.delete("/tariffs/{tariff_id}")
def delete_tariff_api(tariff_id: str, db: Session = Depends(get_db)):
    delete_tariff(db, tariff_id)
    return {"status": "deleted"}

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
