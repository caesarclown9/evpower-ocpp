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
from app.crud.ocpp_service import OCPPStationService, OCPPAuthorizationService

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
async def start_charging(request: StartChargingRequest):
    """
    🚀 Начать зарядку - главная кнопка в приложении
    
    Что происходит:
    1. Проверяем что станция онлайн
    2. Проверяем что коннектор свободен
    3. Генерируем короткий ID для клиента
    4. Отправляем команду станции через OCPP
    5. Возвращаем статус
    """
    try:
        # 1. Проверяем что станция подключена
        connected_stations = await redis_manager.get_stations()
        if request.station_id not in connected_stations:
            return {
                "success": False,
                "error": "station_offline",
                "message": "Станция недоступна"
            }
        
        # 2. Генерируем уникальную сессию и короткий idTag
        session_id = str(uuid.uuid4())[:8]  # короткий ID
        id_tag = f"CL{request.client_id[-6:]}"  # CLIENT + последние 6 цифр
        
        # 3. Авторизуем клиента в БД OCPP
        with next(get_db()) as db:
            # Добавляем/обновляем авторизацию
            OCPPAuthorizationService.authorize_client(
                db, id_tag, request.client_id, "Accepted"
            )
        
        # 4. Отправляем команду станции
        command_result = await redis_manager.publish_command(request.station_id, {
            "command": "RemoteStartTransaction",
            "payload": {
                "connectorId": request.connector_id,
                "idTag": id_tag
            }
        })
        
        if command_result == 0:  # Нет подписчиков
            return {
                "success": False,
                "error": "station_not_listening",
                "message": "Станция не отвечает"
            }
        
        # 5. Успешно отправили команду
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
async def stop_charging(request: StopChargingRequest):
    """
    🛑 Остановить зарядку
    """
    try:
        # Ищем активную транзакцию клиента на станции
        with next(get_db()) as db:
            # Находим последнюю активную транзакцию
            from app.models.ocpp import OCPPTransaction
            active_transaction = db.query(OCPPTransaction).filter(
                OCPPTransaction.station_id == request.station_id,
                OCPPTransaction.id_tag.like(f"%{request.client_id[-6:]}%"),
                OCPPTransaction.status == "started"
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
async def get_charging_status(request: ChargingStatusRequest):
    """
    📊 Проверить статус зарядки
    
    Возвращает текущее состояние зарядки клиента
    """
    try:
        with next(get_db()) as db:
            # Ищем транзакцию клиента
            from app.models.ocpp import OCPPTransaction, OCPPMeterValues
            
            transaction = db.query(OCPPTransaction).filter(
                OCPPTransaction.station_id == request.station_id,
                OCPPTransaction.id_tag.like(f"%{request.client_id[-6:]}%")
            ).order_by(OCPPTransaction.start_timestamp.desc()).first()
            
            if not transaction:
                return {
                    "success": True,
                    "status": "no_transaction",
                    "message": "Зарядка не найдена"
                }
            
            # Получаем последние показания счетчика
            latest_meter = db.query(OCPPMeterValues).filter(
                OCPPMeterValues.station_id == request.station_id,
                OCPPMeterValues.transaction_id == transaction.transaction_id
            ).order_by(OCPPMeterValues.timestamp.desc()).first()
            
            # Рассчитываем данные
            energy_delivered = 0
            current_cost = 0
            
            if latest_meter and latest_meter.sampled_values:
                for sample in latest_meter.sampled_values:
                    if sample.get('measurand') == 'Energy.Active.Import.Register':
                        energy_delivered = float(sample.get('value', 0)) / 1000  # Wh -> kWh
                        current_cost = energy_delivered * 14.95  # тариф с экрана
                        break
            
            return {
                "success": True,
                "status": transaction.status,  # "started" или "completed"
                "transaction_id": transaction.transaction_id,
                "connector_id": transaction.connector_id,
                "start_time": transaction.start_timestamp.isoformat(),
                "stop_time": transaction.stop_timestamp.isoformat() if transaction.stop_timestamp else None,
                "energy_delivered_kwh": round(energy_delivered, 2),
                "current_cost_rub": round(current_cost, 2),
                "meter_start": transaction.meter_start,
                "meter_stop": transaction.meter_stop,
                "message": "Зарядка активна" if transaction.status == "started" else "Зарядка завершена"
            }
            
    except Exception as e:
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка: {str(e)}"
        }

@router.post("/station/status") 
async def get_station_status(request: StationStatusRequest):
    """
    🏢 Статус станции и коннекторов
    
    Использует автоматические каскадные статусы из БД
    """
    try:
        # Проверяем подключение станции к WebSocket
        connected_stations = await redis_manager.get_stations()
        is_online = request.station_id in connected_stations
        
        with next(get_db()) as db:
            # Используем новое представление с каскадными статусами
            station_query = """
                SELECT 
                    station_id,
                    serial_number,
                    station_status,
                    location_name,
                    location_status,
                    location_address,
                    total_connectors,
                    available_connectors,
                    occupied_connectors,
                    faulted_connectors,
                    is_available_for_charging,
                    price_per_kwh,
                    session_fee,
                    currency
                FROM station_status_summary 
                WHERE station_id = %s
            """
            cursor = db.execute(station_query, (request.station_id,))
            station_data = cursor.fetchone()
            
            if not station_data:
                return {
                    "success": False,
                    "error": "station_not_found",
                    "message": "Станция не найдена"
                }
            
            # Получаем детальную информацию о коннекторах
            connectors_query = """
                SELECT connector_number, connector_type, power_kw, status, error_code
                FROM connectors 
                WHERE station_id = %s 
                ORDER BY connector_number
            """
            cursor = db.execute(connectors_query, (request.station_id,))
            db_connectors = cursor.fetchall()
            
            # Формируем детальный статус коннекторов
            connectors = []
            for conn in db_connectors:
                connector_number = conn[0]
                
                connectors.append({
                    "id": connector_number,
                    "type": conn[1],  # connector_type
                    "status": "Свободен" if conn[3] == "Available" 
                             else "Занят" if conn[3] == "Occupied" 
                             else "Неисправен",
                    "available": conn[3] == "Available" and is_online,
                    "power_kw": conn[2],  # power_kw
                    "error": conn[4] if conn[4] != "NoError" else None
                })
            
            # Конвертируем цену (может быть Decimal)
            price_per_kwh = float(station_data[11]) if station_data[11] else 14.95
            session_fee = float(station_data[12]) if station_data[12] else 0.0
            
            return {
                "success": True,
                "station_id": request.station_id,
                "serial_number": station_data[1],
                
                # Статусы (автоматические из БД)
                "online": is_online,
                "station_status": station_data[2],  # active/maintenance/inactive
                "location_status": station_data[4],  # active/maintenance/inactive
                "available_for_charging": station_data[10] and is_online,
                
                # Локация
                "location_name": station_data[3],
                "location_address": station_data[5],
                
                # Коннекторы (детально)
                "connectors": connectors,
                "total_connectors": station_data[6],
                "available_connectors": station_data[7] if is_online else 0,
                "occupied_connectors": station_data[8],
                "faulted_connectors": station_data[9],
                
                # Тарифы
                "tariff_rub_kwh": price_per_kwh,
                "session_fee": session_fee,
                "currency": station_data[13] or "KGS",
                "working_hours": "Круглосуточно",
                
                "message": "Станция работает" if is_online and station_data[2] == "active" 
                          else "Станция на обслуживании" if station_data[2] == "maintenance"
                          else "Станция недоступна"
            }
            
    except Exception as e:
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка: {str(e)}"
        } 