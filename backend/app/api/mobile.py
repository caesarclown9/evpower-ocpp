"""
Mobile API для FlutterFlow приложения
Простые POST endpoints для управления зарядкой
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import uuid
import logging
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
logger = logging.getLogger(__name__)

# ============================================================================
# МОДЕЛИ ДАННЫХ
# ============================================================================

class ChargingStartRequest(BaseModel):
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
async def start_charging(request: ChargingStartRequest, db: Session = Depends(get_db)):
    """
    Запуск зарядки через mobile приложение
    Автоматически создает авторизацию для клиента если её нет
    """
    try:
        # 1. Проверяем существование станции
        station_query = """
            SELECT s.*, l.name as location_name, l.address as location_address 
            FROM stations s 
            JOIN locations l ON s.location_id = l.id 
            WHERE s.id = :station_id AND s.status = 'active'
        """
        station_result = db.execute(text(station_query), {"station_id": request.station_id})
        station = station_result.fetchone()
        
        if not station:
            return {
                "success": False, 
                "error": "station_not_found",
                "message": "Станция не найдена или неактивна"
            }
        
        # 2. Проверяем существование клиента
        client_query = "SELECT * FROM clients WHERE id = :client_id"
        client_result = db.execute(text(client_query), {"client_id": request.client_id})
        client = client_result.fetchone()
        
        if not client:
            return {
                "success": False,
                "error": "client_not_found", 
                "message": "Клиент не найден"
            }
        
        # 3. Проверяем доступность коннектора
        connector_query = """
            SELECT * FROM connectors 
            WHERE station_id = :station_id 
            AND connector_number = :connector_id 
            AND status = 'Available'
        """
        connector_result = db.execute(text(connector_query), {
            "station_id": request.station_id,
            "connector_id": request.connector_id
        })
        connector = connector_result.fetchone()
        
        if not connector:
            return {
                "success": False,
                "error": "connector_unavailable",
                "message": "Коннектор недоступен"
            }
        
        # 4. Проверяем нет ли активной зарядки у клиента
        active_session_query = """
            SELECT * FROM charging_sessions 
            WHERE client_id = :client_id 
            AND status IN ('active', 'preparing', 'charging')
        """
        active_session_result = db.execute(text(active_session_query), {"client_id": request.client_id})
        active_session = active_session_result.fetchone()
        
        if active_session:
            return {
                "success": False,
                "error": "session_already_active",
                "message": "У клиента уже есть активная зарядка"
            }
        
        # 5. Проверяем онлайн ли станция через Redis
        try:
            stations = await redis_manager.get_stations()
            if request.station_id not in stations:
                return {
                    "success": False,
                    "error": "station_offline", 
                    "message": "Станция недоступна"
                }
        except Exception as e:
            logger.error(f"Redis error: {e}")
            return {
                "success": False,
                "error": "station_offline",
                "message": "Станция недоступна"
            }
        
        # 6. 🆕 АВТОМАТИЧЕСКАЯ АВТОРИЗАЦИЯ: Создаем или получаем авторизацию для клиента
        id_tag = f"CLIENT_{request.client_id}"
        
        auth_check_query = "SELECT * FROM ocpp_authorization WHERE client_id = :client_id"
        auth_result = db.execute(text(auth_check_query), {"client_id": request.client_id})
        auth_record = auth_result.fetchone()
        
        if not auth_record:
            # Создаем новую авторизацию для клиента
            auth_insert_query = """
                INSERT INTO ocpp_authorization (id_tag, status, client_id, created_at, updated_at)
                VALUES (:id_tag, 'Accepted', :client_id, NOW(), NOW())
            """
            db.execute(text(auth_insert_query), {
                "id_tag": id_tag,
                "client_id": request.client_id
            })
            logger.info(f"Создана авторизация для клиента {request.client_id} с id_tag {id_tag}")
        else:
            id_tag = auth_record.id_tag
            logger.info(f"Используется существующая авторизация {id_tag} для клиента {request.client_id}")
        
        # 7. Создаем сессию зарядки
        session_id = f"CS_{request.station_id}_{request.client_id}_{int(datetime.now().timestamp())}"
        
        session_insert_query = """
            INSERT INTO charging_sessions (
                id, station_id, client_id, connector_id, 
                start_time, status, energy_limit_kwh, amount_limit_rub,
                created_at, updated_at
            ) VALUES (
                :session_id, :station_id, :client_id, :connector_id,
                NOW(), 'preparing', :energy_kwh, :amount_rub,
                NOW(), NOW()
            )
        """
        
        db.execute(text(session_insert_query), {
            "session_id": session_id,
            "station_id": request.station_id,
            "client_id": request.client_id,
            "connector_id": request.connector_id,
            "energy_kwh": request.energy_kwh,
            "amount_rub": request.amount_rub
        })
        
        # 8. Обновляем статус коннектора
        update_connector_query = """
            UPDATE connectors 
            SET status = 'Preparing', last_status_update = NOW()
            WHERE station_id = :station_id AND connector_number = :connector_id
        """
        db.execute(text(update_connector_query), {
            "station_id": request.station_id,
            "connector_id": request.connector_id
        })
        
        db.commit()
        
        # 9. Отправляем команду станции через Redis
        command = {
            "command": "RemoteStartTransaction",
            "payload": {
                "connectorId": request.connector_id,
                "idTag": id_tag,
                "session_id": session_id,
                "energy_limit": request.energy_kwh
            }
        }
        
        await redis_manager.publish_command(request.station_id, command)
        logger.info(f"Отправлена команда RemoteStartTransaction для {request.station_id}")
        
        return {
            "success": True,
            "session_id": session_id,
            "id_tag": id_tag,
            "message": "Команда запуска отправлена на станцию",
            "station_name": station.location_name,
            "connector_id": request.connector_id,
            "energy_limit": request.energy_kwh,
            "amount_limit": request.amount_rub
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при запуске зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка сервера: {str(e)}"
        }

@router.post("/charging/stop")
async def stop_charging(request: StopChargingRequest, db: Session = Depends(get_db)):
    """🛑 Остановить зарядку"""
    try:
        # Ищем активную сессию клиента на станции
        active_session_query = """
            SELECT * FROM charging_sessions 
            WHERE station_id = :station_id 
            AND client_id = :client_id 
            AND status IN ('preparing', 'active', 'charging')
            ORDER BY start_time DESC
            LIMIT 1
        """
        
        session_result = db.execute(text(active_session_query), {
            "station_id": request.station_id,
            "client_id": request.client_id
        })
        active_session = session_result.fetchone()
        
        if not active_session:
            return {
                "success": False,
                "error": "no_active_transaction",
                "message": "Активная зарядка не найдена"
            }
        
        session_id = active_session[0]  # id поле
        
        # Получаем авторизацию клиента для отправки команды
        auth_query = "SELECT id_tag FROM ocpp_authorization WHERE client_id = :client_id"
        auth_result = db.execute(text(auth_query), {"client_id": request.client_id})
        auth_record = auth_result.fetchone()
        
        if not auth_record:
            return {
                "success": False,
                "error": "client_not_authorized",
                "message": "Клиент не авторизован"
            }
        
        # Обновляем статус сессии
        update_session_query = """
            UPDATE charging_sessions 
            SET status = 'stopping', updated_at = NOW()
            WHERE id = :session_id
        """
        db.execute(text(update_session_query), {"session_id": session_id})
        db.commit()
        
        # Отправляем команду остановки через Redis
        command = {
            "command": "RemoteStopTransaction",
            "payload": {
                "session_id": session_id,
                "client_id": request.client_id
            }
        }
        
        await redis_manager.publish_command(request.station_id, command)
        logger.info(f"Отправлена команда RemoteStopTransaction для клиента {request.client_id}")
        
        return {
            "success": True,
            "session_id": session_id,
            "message": "Команда остановки отправлена"
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при остановке зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error", 
            "message": f"Ошибка: {str(e)}"
        }

@router.post("/charging/status")
async def get_charging_status(request: ChargingStatusRequest, db: Session = Depends(get_db)):
    """📊 Проверить статус зарядки"""
    try:
        # Ищем сессию клиента на станции
        session_query = """
            SELECT * FROM charging_sessions 
            WHERE station_id = :station_id 
            AND client_id = :client_id 
            ORDER BY start_time DESC
            LIMIT 1
        """
        
        session_result = db.execute(text(session_query), {
            "station_id": request.station_id,
            "client_id": request.client_id
        })
        session = session_result.fetchone()
        
        if not session:
            return {
                "success": True,
                "status": "no_transaction",
                "message": "Зарядка не найдена"
            }
        
        # Получаем данные сессии
        session_id = session[0]  # id
        station_id = session[1]  # station_id
        client_id = session[2]  # client_id
        connector_id = session[3]  # connector_id
        start_time = session[4]  # start_time
        stop_time = session[5]  # stop_time
        status = session[6]  # status
        energy_consumed = session[7] or 0  # energy_consumed_kwh
        amount_charged = session[8] or 0  # amount_charged_rub
        energy_limit = session[9] or 0  # energy_limit_kwh
        amount_limit = session[10] or 0  # amount_limit_rub
        
        # Если зарядка активна, пытаемся получить реальные показания счетчика
        if status in ['preparing', 'active', 'charging']:
            # Здесь можно добавить логику получения реальных данных из OCPP транзакций
            # Пока используем данные из charging_sessions
            pass
        
        return {
            "success": True,
            "status": status,
            "session_id": session_id,
            "connector_id": connector_id,
            "start_time": start_time.isoformat() if start_time else None,
            "stop_time": stop_time.isoformat() if stop_time else None,
            "energy_delivered_kwh": round(float(energy_consumed), 2),
            "amount_charged_rub": round(float(amount_charged), 2),
            "energy_limit_kwh": round(float(energy_limit), 2),
            "amount_limit_rub": round(float(amount_limit), 2),
            "message": "Зарядка активна" if status in ['preparing', 'active', 'charging'] 
                      else "Зарядка завершена" if status == 'completed'
                      else "Зарядка остановлена"
        }
        
    except Exception as e:
        logger.error(f"Ошибка при получении статуса зарядки: {e}")
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