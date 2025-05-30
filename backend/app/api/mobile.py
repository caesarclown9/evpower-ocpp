"""
Mobile API для FlutterFlow приложения
Простые POST endpoints для управления зарядкой
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import uuid
from datetime import datetime

from ocpp_ws_server.redis_manager import redis_manager
from app.db.session import get_db
from app.db.models.ocpp import (
    Station, Location, OCPPStationStatus, OCPPTransaction, 
    OCPPMeterValue, ChargingSession, OCPPAuthorization
)
from sqlalchemy.orm import Session
from sqlalchemy import text

router = APIRouter(prefix="/api/mobile", tags=["mobile"])

# ============================================================================
# МОДЕЛИ ДАННЫХ
# ============================================================================

class StartChargingRequest(BaseModel):
    station_id: str
    client_id: str
    connector_id: int
    energy_kwh: float  # сколько кВт⋅ч заказал клиент
    amount_rub: float  # сколько заплатил клиент

class StopChargingRequest(BaseModel):
    station_id: str
    client_id: str
    session_id: Optional[str] = None

class ChargingStatusRequest(BaseModel):
    station_id: str
    client_id: str
    session_id: Optional[str] = None

class StationStatusRequest(BaseModel):
    station_id: str

# ============================================================================
# ОСНОВНЫЕ ENDPOINTS (только POST для простоты)
# ============================================================================

@router.post("/charging/start")
async def start_charging(request: StartChargingRequest, db: Session = Depends(get_db)):
    """🚀 Начать зарядку - главная кнопка в приложении"""
    try:
        # 1. Проверяем что станция существует в БД
        station = db.query(Station).filter(Station.id == request.station_id).first()
        if not station:
            return {
                "success": False,
                "error": "station_not_found",
                "message": "Станция не найдена в БД"
            }
        
        # 2. Проверяем что станция подключена
        connected_stations = await redis_manager.get_stations()
        if request.station_id not in connected_stations:
            return {
                "success": False,
                "error": "station_offline",
                "message": "Станция недоступна"
            }
        
        # 3. Генерируем уникальную сессию и короткий idTag
        session_id = f"CS_{request.station_id}_{str(uuid.uuid4())[:8]}"
        id_tag = f"CL{request.client_id[-6:]}"
        
        # 4. Авторизуем клиента в БД OCPP
        existing_auth = db.query(OCPPAuthorization).filter(
            OCPPAuthorization.id_tag == id_tag
        ).first()
        
        if not existing_auth:
            new_auth = OCPPAuthorization(
                id_tag=id_tag,
                status="Accepted",
                user_id=request.client_id
            )
            db.add(new_auth)
            db.commit()
        
        # 5. Отправляем команду станции
        command_result = await redis_manager.publish_command(request.station_id, {
            "command": "RemoteStartTransaction",
            "payload": {
                "connectorId": request.connector_id,
                "idTag": id_tag
            }
        })
        
        if command_result == 0:
            return {
                "success": False,
                "error": "station_not_listening",
                "message": "Станция не отвечает"
            }
        
        return {
            "success": True,
            "session_id": session_id,
            "id_tag": id_tag,
            "station_id": request.station_id,
            "connector_id": request.connector_id,
            "energy_kwh": request.energy_kwh,
            "amount_rub": request.amount_rub,
            "status": "command_sent",
            "message": "Команда отправлена станции. Ожидайте начала зарядки..."
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка сервера: {str(e)}"
        }

@router.post("/charging/stop")
async def stop_charging(request: StopChargingRequest, db: Session = Depends(get_db)):
    """🛑 Остановить зарядку"""
    try:
        # Ищем активную транзакцию клиента на станции
        id_tag_pattern = f"%{request.client_id[-6:]}%"
        
        active_transaction = db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == request.station_id,
            OCPPTransaction.id_tag.like(id_tag_pattern),
            OCPPTransaction.status == "Started"
        ).order_by(OCPPTransaction.start_timestamp.desc()).first()
        
        if not active_transaction:
            return {
                "success": False,
                "error": "no_active_transaction",
                "message": "Активная зарядка не найдена"
            }
        
        transaction_id = active_transaction.transaction_id
        
        # Отправляем команду остановки
        command_result = await redis_manager.publish_command(request.station_id, {
            "command": "RemoteStopTransaction", 
            "payload": {
                "transactionId": transaction_id
            }
        })
        
        if command_result == 0:
            return {
                "success": False,
                "error": "station_not_listening",
                "message": "Станция не отвечает"
            }
        
        return {
            "success": True,
            "transaction_id": transaction_id,
            "message": "Команда остановки отправлена"
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": "internal_error", 
            "message": f"Ошибка: {str(e)}"
        }

@router.post("/charging/status")
async def get_charging_status(request: ChargingStatusRequest, db: Session = Depends(get_db)):
    """📊 Проверить статус зарядки"""
    try:
        # Ищем транзакцию клиента
        id_tag_pattern = f"%{request.client_id[-6:]}%"
        
        transaction = db.query(OCPPTransaction).filter(
            OCPPTransaction.station_id == request.station_id,
            OCPPTransaction.id_tag.like(id_tag_pattern)
        ).order_by(OCPPTransaction.start_timestamp.desc()).first()
        
        if not transaction:
            return {
                "success": True,
                "status": "no_transaction",
                "message": "Зарядка не найдена"
            }
        
        # Получаем последние показания счетчика
        latest_meter = db.query(OCPPMeterValue).filter(
            OCPPMeterValue.station_id == request.station_id,
            OCPPMeterValue.transaction_id == transaction.transaction_id
        ).order_by(OCPPMeterValue.timestamp.desc()).first()
        
        # Рассчитываем данные
        energy_delivered = 0
        current_cost = 0
        
        if latest_meter and latest_meter.energy_active_import_register:
            energy_delivered = float(latest_meter.energy_active_import_register)
            current_cost = energy_delivered * 14.95  # базовый тариф
        
        return {
            "success": True,
            "status": transaction.status,
            "transaction_id": transaction.transaction_id,
            "connector_id": transaction.connector_id,
            "start_time": transaction.start_timestamp.isoformat(),
            "stop_time": transaction.stop_timestamp.isoformat() if transaction.stop_timestamp else None,
            "energy_delivered_kwh": round(energy_delivered, 2),
            "current_cost_rub": round(current_cost, 2),
            "meter_start": float(transaction.meter_start) if transaction.meter_start else 0,
            "meter_stop": float(transaction.meter_stop) if transaction.meter_stop else None,
            "message": "Зарядка активна" if transaction.status == "Started" else "Зарядка завершена"
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка: {str(e)}"
        }

@router.post("/station/status") 
async def get_station_status(request: StationStatusRequest, db: Session = Depends(get_db)):
    """🏢 Статус станции и коннекторов"""
    try:
        # Получаем данные станции с локацией через JOIN
        result = db.execute(text("""
            SELECT 
                s.id,
                s.serial_number,
                s.model,
                s.manufacturer,
                s.status,
                s.power_capacity,
                s.connector_types,
                s.connectors_count,
                s.price_per_kwh,
                s.session_fee,
                s.currency,
                l.name as location_name,
                l.address as location_address,
                l.status as location_status
            FROM stations s
            LEFT JOIN locations l ON s.location_id = l.id
            WHERE s.id = :station_id
        """), {"station_id": request.station_id})
        
        station_data = result.fetchone()
        
        if not station_data:
            return {
                "success": False,
                "error": "station_not_found",
                "message": "Станция не найдена"
            }
        
        # Проверяем подключение станции
        connected_stations = await redis_manager.get_stations()
        is_online = request.station_id in connected_stations
        
        # Получаем статус коннекторов
        connectors_result = db.execute(text("""
            SELECT connector_number, connector_type, power_kw, status, error_code
            FROM connectors 
            WHERE station_id = :station_id 
            ORDER BY connector_number
        """), {"station_id": request.station_id})
        
        connectors = []
        available_count = 0
        occupied_count = 0
        faulted_count = 0
        
        for conn in connectors_result.fetchall():
            connector_status = conn[3]  # status
            connector_available = connector_status.lower() == "available" and is_online
            
            if connector_available:
                available_count += 1
            elif connector_status.lower() == "occupied":
                occupied_count += 1
            else:
                faulted_count += 1
            
            connectors.append({
                "id": conn[0],  # connector_number
                "type": conn[1],  # connector_type
                "status": "Свободен" if connector_status.lower() == "available" 
                         else "Занят" if connector_status.lower() == "occupied" 
                         else "Неисправен",
                "available": connector_available,
                "power_kw": conn[2],  # power_kw
                "error": conn[4] if conn[4] and conn[4] != "NoError" else None
            })
        
        # Формируем ответ
        return {
            "success": True,
            "station_id": request.station_id,
            "serial_number": station_data[1],
            "model": station_data[2],
            "manufacturer": station_data[3],
            
            # Статусы
            "online": is_online,
            "station_status": station_data[4],  # active/maintenance/inactive
            "location_status": station_data[13],  # active/maintenance/inactive
            "available_for_charging": is_online and station_data[4] == "active" and available_count > 0,
            
            # Локация
            "location_name": station_data[11],
            "location_address": station_data[12],
            
            # Коннекторы
            "connectors": connectors,
            "total_connectors": station_data[7],  # connectors_count
            "available_connectors": available_count,
            "occupied_connectors": occupied_count,
            "faulted_connectors": faulted_count,
            
            # Тарифы
            "tariff_rub_kwh": float(station_data[8]) if station_data[8] else 14.95,
            "session_fee": float(station_data[9]) if station_data[9] else 0.0,
            "currency": station_data[10] or "KGS",
            "working_hours": "Круглосуточно",
            
            "message": "Станция работает" if is_online and station_data[4] == "active" 
                      else "Станция на обслуживании" if station_data[4] == "maintenance"
                      else "Станция недоступна"
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка: {str(e)}"
        } 