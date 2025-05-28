from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

from app.db.session import get_db
from app.crud.ocpp_service import (
    OCPPStationService,
    OCPPTransactionService,
    OCPPMeterService,
    OCPPAuthorizationService,
    OCPPConfigurationService
)
from app.db.models.ocpp import (
    OCPPStationStatus,
    OCPPTransaction,
    OCPPMeterValue,
    OCPPAuthorization,
    OCPPConfiguration
)

router = APIRouter()

# === Station Status Endpoints ===

@router.get("/stations/status", response_model=List[Dict[str, Any]])
async def get_all_stations_status(db: Session = Depends(get_db)):
    """Получение статуса всех станций"""
    try:
        online_stations = OCPPStationService.get_online_stations(db)
        return [
            {
                "station_id": station.station_id,
                "status": station.status,
                "error_code": station.error_code,
                "is_online": station.is_online,
                "last_heartbeat": station.last_heartbeat.isoformat() if station.last_heartbeat else None,
                "firmware_version": station.firmware_version,
                "connector_status": station.connector_status
            }
            for station in online_stations
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching station status: {str(e)}")

@router.get("/stations/{station_id}/status")
async def get_station_status(station_id: str, db: Session = Depends(get_db)):
    """Получение статуса конкретной станции"""
    try:
        status = OCPPStationService.get_station_status(db, station_id)
        if not status:
            raise HTTPException(status_code=404, detail="Station status not found")
        
        return {
            "station_id": status.station_id,
            "status": status.status,
            "error_code": status.error_code,
            "info": status.info,
            "is_online": status.is_online,
            "last_heartbeat": status.last_heartbeat.isoformat() if status.last_heartbeat else None,
            "firmware_version": status.firmware_version,
            "boot_notification_sent": status.boot_notification_sent,
            "connector_status": status.connector_status,
            "created_at": status.created_at.isoformat() if status.created_at else None,
            "updated_at": status.updated_at.isoformat() if status.updated_at else None
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching station status: {str(e)}")

# === Transaction Endpoints ===

@router.get("/stations/{station_id}/transactions", response_model=List[Dict[str, Any]])
async def get_station_transactions(
    station_id: str, 
    limit: int = 10,
    db: Session = Depends(get_db)
):
    """Получение транзакций станции"""
    try:
        transactions = db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id
        ).order_by(OCPPTransaction.start_timestamp.desc()).limit(limit).all()
        
        return [
            {
                "id": tx.id,
                "transaction_id": tx.transaction_id,
                "station_id": tx.station_id,
                "connector_id": tx.connector_id,
                "id_tag": tx.id_tag,
                "meter_start": float(tx.meter_start) if tx.meter_start else 0,
                "meter_stop": float(tx.meter_stop) if tx.meter_stop else None,
                "start_timestamp": tx.start_timestamp.isoformat(),
                "stop_timestamp": tx.stop_timestamp.isoformat() if tx.stop_timestamp else None,
                "stop_reason": tx.stop_reason,
                "status": tx.status,
                "energy_delivered": float(tx.meter_stop - tx.meter_start) if tx.meter_stop and tx.meter_start else None,
                "charging_session_id": tx.charging_session_id
            }
            for tx in transactions
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching transactions: {str(e)}")

@router.get("/stations/{station_id}/active-transaction")
async def get_active_transaction(station_id: str, db: Session = Depends(get_db)):
    """Получение активной транзакции станции"""
    try:
        transaction = OCPPTransactionService.get_active_transaction(db, station_id)
        if not transaction:
            return {"message": "No active transaction"}
        
        return {
            "transaction_id": transaction.transaction_id,
            "station_id": transaction.station_id,
            "connector_id": transaction.connector_id,
            "id_tag": transaction.id_tag,
            "meter_start": float(transaction.meter_start),
            "start_timestamp": transaction.start_timestamp.isoformat(),
            "status": transaction.status,
            "charging_session_id": transaction.charging_session_id,
            "duration_minutes": int((datetime.utcnow() - transaction.start_timestamp).total_seconds() / 60)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching active transaction: {str(e)}")

# === Meter Values Endpoints ===

@router.get("/stations/{station_id}/meter-values", response_model=List[Dict[str, Any]])
async def get_station_meter_values(
    station_id: str,
    limit: int = 20,
    hours: int = 24,
    db: Session = Depends(get_db)
):
    """Получение показаний счетчика станции"""
    try:
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        
        meter_values = db.query(OCPPMeterValue).filter(
            OCPPMeterValue.station_id == station_id,
            OCPPMeterValue.timestamp >= cutoff_time
        ).order_by(OCPPMeterValue.timestamp.desc()).limit(limit).all()
        
        return [
            {
                "id": mv.id,
                "transaction_id": mv.transaction_id,
                "connector_id": mv.connector_id,
                "timestamp": mv.timestamp.isoformat(),
                "energy_kwh": float(mv.energy_active_import_register) if mv.energy_active_import_register else None,
                "power_w": float(mv.power_active_import) if mv.power_active_import else None,
                "current_a": float(mv.current_import) if mv.current_import else None,
                "voltage_v": float(mv.voltage) if mv.voltage else None,
                "temperature_c": float(mv.temperature) if mv.temperature else None,
                "soc_percent": float(mv.soc) if mv.soc else None,
                "sampled_values": mv.sampled_values
            }
            for mv in meter_values
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching meter values: {str(e)}")

@router.get("/stations/{station_id}/latest-meter-reading")
async def get_latest_meter_reading(station_id: str, db: Session = Depends(get_db)):
    """Получение последних показаний счетчика"""
    try:
        latest = db.query(OCPPMeterValue).filter(
            OCPPMeterValue.station_id == station_id
        ).order_by(OCPPMeterValue.timestamp.desc()).first()
        
        if not latest:
            return {"message": "No meter readings found"}
        
        return {
            "timestamp": latest.timestamp.isoformat(),
            "energy_kwh": float(latest.energy_active_import_register) if latest.energy_active_import_register else None,
            "power_w": float(latest.power_active_import) if latest.power_active_import else None,
            "current_a": float(latest.current_import) if latest.current_import else None,
            "voltage_v": float(latest.voltage) if latest.voltage else None,
            "transaction_id": latest.transaction_id,
            "connector_id": latest.connector_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching latest meter reading: {str(e)}")

# === Authorization Endpoints ===

@router.get("/authorization/tags", response_model=List[Dict[str, Any]])
async def get_all_authorization_tags(db: Session = Depends(get_db)):
    """Получение всех авторизованных тегов"""
    try:
        tags = db.query(OCPPAuthorization).all()
        return [
            {
                "id_tag": tag.id_tag,
                "status": tag.status,
                "user_id": tag.user_id,
                "expiry_date": tag.expiry_date.isoformat() if tag.expiry_date else None,
                "created_at": tag.created_at.isoformat() if tag.created_at else None
            }
            for tag in tags
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching authorization tags: {str(e)}")

@router.post("/authorization/tags")
async def add_authorization_tag(
    id_tag: str,
    status: str = "Accepted",
    user_id: Optional[str] = None,
    expiry_date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Добавление нового авторизованного тега"""
    try:
        expiry_dt = None
        if expiry_date:
            expiry_dt = datetime.fromisoformat(expiry_date)
        
        auth = OCPPAuthorizationService.add_id_tag(
            db, id_tag, status, user_id, expiry_dt
        )
        
        return {
            "message": "Authorization tag added successfully",
            "id_tag": auth.id_tag,
            "status": auth.status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error adding authorization tag: {str(e)}")

@router.get("/authorization/check/{id_tag}")
async def check_authorization(id_tag: str, db: Session = Depends(get_db)):
    """Проверка авторизации тега"""
    try:
        result = OCPPAuthorizationService.authorize_id_tag(db, id_tag)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking authorization: {str(e)}")

# === Configuration Endpoints ===

@router.get("/stations/{station_id}/configuration")
async def get_station_configuration(station_id: str, db: Session = Depends(get_db)):
    """Получение конфигурации станции"""
    try:
        configs = OCPPConfigurationService.get_configuration(db, station_id)
        return {
            config.key: {
                "value": config.value,
                "readonly": config.readonly,
                "updated_at": config.updated_at.isoformat() if config.updated_at else None
            }
            for config in configs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching configuration: {str(e)}")

@router.post("/stations/{station_id}/configuration")
async def set_station_configuration(
    station_id: str,
    key: str,
    value: str,
    readonly: bool = False,
    db: Session = Depends(get_db)
):
    """Установка конфигурации станции"""
    try:
        config = OCPPConfigurationService.set_configuration(
            db, station_id, key, value, readonly
        )
        
        return {
            "message": "Configuration set successfully",
            "key": config.key,
            "value": config.value,
            "readonly": config.readonly
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error setting configuration: {str(e)}")

# === Statistics Endpoints ===

@router.get("/statistics/overview")
async def get_statistics_overview(db: Session = Depends(get_db)):
    """Получение общей статистики OCPP"""
    try:
        # Онлайн станции
        online_stations = OCPPStationService.get_online_stations(db)
        online_count = len(online_stations)
        
        # Активные транзакции
        active_transactions = db.query(OCPPTransaction).filter(
            OCPPTransaction.status == "Started"
        ).count()
        
        # Транзакции за последние 24 часа
        yesterday = datetime.utcnow() - timedelta(hours=24)
        transactions_24h = db.query(OCPPTransaction).filter(
            OCPPTransaction.start_timestamp >= yesterday
        ).count()
        
        # Авторизованные теги
        authorized_tags = db.query(OCPPAuthorization).filter(
            OCPPAuthorization.status == "Accepted"
        ).count()
        
        return {
            "stations_online": online_count,
            "active_transactions": active_transactions,
            "transactions_24h": transactions_24h,
            "authorized_tags": authorized_tags,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching statistics: {str(e)}")

@router.get("/stations/{station_id}/statistics")
async def get_station_statistics(
    station_id: str,
    days: int = 7,
    db: Session = Depends(get_db)
):
    """Получение статистики конкретной станции"""
    try:
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        # Транзакции станции
        transactions = db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == station_id,
            OCPPTransaction.start_timestamp >= cutoff_date
        ).all()
        
        total_energy = sum(
            float(tx.meter_stop - tx.meter_start) 
            for tx in transactions 
            if tx.meter_stop and tx.meter_start
        )
        
        completed_transactions = len([tx for tx in transactions if tx.status == "Stopped"])
        active_transactions = len([tx for tx in transactions if tx.status == "Started"])
        
        # Статус станции
        status = OCPPStationService.get_station_status(db, station_id)
        
        return {
            "station_id": station_id,
            "period_days": days,
            "total_transactions": len(transactions),
            "completed_transactions": completed_transactions,
            "active_transactions": active_transactions,
            "total_energy_kwh": round(total_energy, 2),
            "station_status": status.status if status else "Unknown",
            "is_online": status.is_online if status else False,
            "last_heartbeat": status.last_heartbeat.isoformat() if status and status.last_heartbeat else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching station statistics: {str(e)}") 