"""
📱 Mobile API endpoints для FlutterFlow
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
import logging
from datetime import datetime, timezone

from app.db.session import get_db
from ocpp_ws_server.redis_manager import redis_manager
from pydantic import BaseModel, Field

# Логгер
logger = logging.getLogger(__name__)

# Router
router = APIRouter(prefix="/api", tags=["mobile"])

# ================== Pydantic Models ==================

class ChargingStartRequest(BaseModel):
    """🔌 Запрос на начало зарядки"""
    client_id: str = Field(..., min_length=1, description="ID клиента")
    station_id: str = Field(..., min_length=1, description="ID станции")
    connector_id: int = Field(..., ge=1, description="Номер коннектора")
    energy_kwh: float = Field(..., gt=0, le=200, description="Энергия для зарядки в кВт⋅ч")
    amount_som: float = Field(..., gt=0, description="Предоплаченная сумма в сомах")

class ChargingStopRequest(BaseModel):
    """⏹️ Запрос на остановку зарядки"""
    session_id: str = Field(..., min_length=1, description="ID сессии зарядки")

# ================== API Endpoints ==================

@router.post("/charging/start")
async def start_charging(request: ChargingStartRequest, db: Session = Depends(get_db)):
    """🔌 Начать зарядку"""
    try:
        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        if not client_check.fetchone():
            return {
                "success": False,
                "error": "client_not_found",
                "message": "Клиент не найден"
            }

        # 2. 🆕 СОЗДАЕМ АВТОРИЗАЦИЮ OCPP СРАЗУ (для мобильного приложения)
        id_tag = f"CLIENT_{request.client_id}"
        
        # Проверяем существование авторизации
        auth_check = db.execute(text("""
            SELECT id_tag FROM ocpp_authorization 
            WHERE id_tag = :id_tag
        """), {"id_tag": id_tag})
        
        if not auth_check.fetchone():
            # Создаем новую авторизацию
            db.execute(text("""
                INSERT INTO ocpp_authorization (id_tag, status, parent_id_tag) 
                VALUES (:id_tag, 'Accepted', NULL)
            """), {"id_tag": id_tag})
            logger.info(f"✅ Created OCPP authorization for {id_tag}")

        # 3. Проверяем станцию
        station_check = db.execute(text("""
            SELECT id, status FROM stations 
            WHERE id = :station_id AND status = 'active'
        """), {"station_id": request.station_id})
        
        station = station_check.fetchone()
        if not station:
            return {
                "success": False,
                "error": "station_unavailable",
                "message": "Станция недоступна"
            }

        # 4. Проверяем коннектор
        connector_check = db.execute(text("""
            SELECT connector_number, status FROM connectors 
            WHERE station_id = :station_id AND connector_number = :connector_id
        """), {"station_id": request.station_id, "connector_id": request.connector_id})
        
        connector = connector_check.fetchone()
        if not connector:
            return {
                "success": False,
                "error": "connector_not_found", 
                "message": "Коннектор не найден"
            }
        
        if connector[1] != "available":
            return {
                "success": False,
                "error": "connector_occupied",
                "message": "Коннектор занят или неисправен"
            }

        # 5. Проверяем активные сессии клиента
        active_session_check = db.execute(text("""
            SELECT id FROM charging_sessions 
            WHERE user_id = :client_id AND status = 'started'
        """), {"client_id": request.client_id})
        
        if active_session_check.fetchone():
            return {
                "success": False,
                "error": "session_already_active",
                "message": "У вас уже есть активная сессия зарядки"
            }

        # 6. 🆕 СОЗДАЕМ СЕССИЮ НЕЗАВИСИМО ОТ ПОДКЛЮЧЕНИЯ СТАНЦИИ
        session_insert = db.execute(text("""
            INSERT INTO charging_sessions 
            (user_id, station_id, start_time, status, limit_type, limit_value)
            VALUES (:user_id, :station_id, :start_time, 'started', 'energy', :energy_kwh)
            RETURNING id
        """), {
            "user_id": request.client_id,
            "station_id": request.station_id,
            "start_time": datetime.now(timezone.utc),
            "energy_kwh": request.energy_kwh
        })
        
        session_id = session_insert.fetchone()[0]

        # 7. Обновляем статус коннектора
        db.execute(text("""
            UPDATE connectors 
            SET status = 'occupied' 
            WHERE station_id = :station_id AND connector_number = :connector_id
        """), {"station_id": request.station_id, "connector_id": request.connector_id})

        # 8. Коммитим транзакцию
        db.commit()

        # 9. 🆕 ПРОВЕРЯЕМ ПОДКЛЮЧЕНИЕ СТАНЦИИ (но не блокируем создание сессии)
        connected_stations = await redis_manager.get_stations()
        is_station_online = request.station_id in connected_stations
        
        if is_station_online:
            # 10. Отправляем команду через Redis только если станция онлайн
            command_data = {
                "action": "RemoteStartTransaction",
                "connector_id": request.connector_id,
                "id_tag": id_tag,
                "session_id": session_id,
                "limit_type": 'energy',
                "limit_value": request.energy_kwh
            }
            
            await redis_manager.publish_command(request.station_id, command_data)
            
            logger.info(f"✅ Зарядка запущена: сессия {session_id}, команда отправлена на станцию")
            
            return {
                "success": True,
                "session_id": session_id,
                "station_id": request.station_id,
                "client_id": request.client_id,
                "connector_id": request.connector_id,
                "energy_kwh": request.energy_kwh,
                "amount_som": request.amount_som,
                "message": "Команда запуска отправлена на станцию",
                "station_online": True
            }
        else:
            logger.info(f"✅ Зарядка создана: сессия {session_id}, станция оффлайн - команда будет отправлена при подключении")
            
            return {
                "success": True,
                "session_id": session_id,
                "station_id": request.station_id,
                "client_id": request.client_id,
                "connector_id": request.connector_id,
                "energy_kwh": request.energy_kwh,
                "amount_som": request.amount_som,
                "message": "Сессия зарядки создана. Станция пока не подключена - зарядка начнется автоматически при подключении.",
                "station_online": False
            }

    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при запуске зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка: {str(e)}"
        }

@router.post("/charging/stop")
async def stop_charging(request: ChargingStopRequest, db: Session = Depends(get_db)):
    """⏹️ Остановить зарядку"""
    try:
        # 1. Ищем активную сессию по session_id
        session_query = """
            SELECT cs.id, cs.station_id, cs.user_id, cs.transaction_id, cs.status
            FROM charging_sessions cs
            WHERE cs.id = :session_id 
            AND cs.status = 'started'
        """
        
        session_result = db.execute(text(session_query), {
            "session_id": request.session_id
        })
        session = session_result.fetchone()
        
        if not session:
            return {
                "success": False,
                "error": "no_active_session",
                "message": "Активная сессия зарядки не найдена"
            }

        session_id = session[0]
        station_id = session[1]
        user_id = session[2]
        transaction_id = session[3]

        # 2. Проверяем подключение станции
        connected_stations = await redis_manager.get_stations()
        if station_id not in connected_stations:
            return {
                "success": False,
                "error": "station_offline", 
                "message": "Станция не подключена"
            }

        # 3. Отправляем команду остановки через Redis
        command_data = {
            "action": "RemoteStopTransaction",
            "transaction_id": transaction_id,
            "session_id": session_id
        }
        
        await redis_manager.publish_command(station_id, command_data)
        
        logger.info(f"🛑 Команда остановки отправлена: сессия {session_id}, транзакция {transaction_id}")
        
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

@router.get("/charging/status/{session_id}")
async def get_charging_status(session_id: str, db: Session = Depends(get_db)):
    """📊 Проверить статус зарядки"""
    try:
        # Ищем сессию по ID
        session_query = """
            SELECT * FROM charging_sessions 
            WHERE id = :session_id
        """
        
        session_result = db.execute(text(session_query), {
            "session_id": session_id
        })
        session = session_result.fetchone()
        
        if not session:
            return {
                "success": False,
                "error": "session_not_found",
                "message": "Сессия зарядки не найдена"
            }
        
        # Получаем данные сессии (по структуре реальной таблицы)
        session_id = session[0]  # id
        user_id = session[1]  # user_id  
        station_id = session[2]  # station_id
        start_time = session[3]  # start_time
        stop_time = session[4]  # stop_time
        energy_consumed = session[5] or 0  # energy
        amount_charged = session[6] or 0  # amount
        status = session[7]  # status
        transaction_id = session[8]  # transaction_id
        limit_type = session[9]  # limit_type
        limit_value = session[10] or 0  # limit_value
        
        # Рассчитываем прогресс и оставшееся время
        progress_percent = 0
        estimated_completion = None
        
        if limit_type == "energy" and limit_value > 0:
            progress_percent = min(100, (float(energy_consumed) / float(limit_value)) * 100)
        elif limit_type == "amount" and limit_value > 0:
            progress_percent = min(100, (float(amount_charged) / float(limit_value)) * 100)
        
        # Длительность в минутах
        duration_minutes = 0
        if start_time:
            if stop_time:
                duration_minutes = int((stop_time - start_time).total_seconds() / 60)
            else:
                duration_minutes = int((datetime.now(timezone.utc) - start_time).total_seconds() / 60)
        
        return {
            "success": True,
            "session_id": session_id,
            "status": status,
            "start_time": start_time.isoformat() if start_time else None,
            "stop_time": stop_time.isoformat() if stop_time else None,
            "duration_minutes": duration_minutes,
            "current_energy": round(float(energy_consumed), 2),
            "current_amount": round(float(amount_charged), 2),
            "limit_type": limit_type,
            "limit_value": round(float(limit_value), 2),
            "progress_percent": round(progress_percent, 1),
            "transaction_id": transaction_id,
            "station_id": station_id,
            "client_id": user_id,
            "message": "Зарядка активна" if status == 'started' 
                      else "Зарядка завершена" if status == 'stopped'
                      else "Ошибка зарядки"
        }
        
    except Exception as e:
        logger.error(f"Ошибка при получении статуса зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка: {str(e)}"
        }

@router.get("/station/status/{station_id}") 
async def get_station_status(station_id: str, db: Session = Depends(get_db)):
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
        """), {"station_id": station_id})
        
        station_data = result.fetchone()
        
        if not station_data:
            return {
                "success": False,
                "error": "station_not_found",
                "message": "Станция не найдена"
            }
        
        # Проверяем подключение станции
        connected_stations = await redis_manager.get_stations()
        is_online = station_id in connected_stations
        
        # Получаем статус коннекторов
        connectors_result = db.execute(text("""
            SELECT connector_number, connector_type, power_kw, status, error_code
            FROM connectors 
            WHERE station_id = :station_id 
            ORDER BY connector_number
        """), {"station_id": station_id})
        
        connectors = []
        available_count = 0
        occupied_count = 0
        faulted_count = 0
        
        for conn in connectors_result.fetchall():
            connector_status = conn[3]  # status
            
            # Упрощенные статусы коннекторов (3 основных)
            if connector_status == "available":
                connector_available = is_online  # доступен только если станция онлайн
                available_count += 1
                status_text = "Свободен"
            elif connector_status == "occupied":
                connector_available = False
                occupied_count += 1
                status_text = "Занят"
            elif connector_status == "faulted":
                connector_available = False
                faulted_count += 1
                status_text = "Неисправен"
            else:
                # Неизвестный статус - считаем неисправным
                connector_available = False
                faulted_count += 1
                status_text = "Неизвестно"
            
            connectors.append({
                "id": conn[0],  # connector_number
                "type": conn[1],  # connector_type
                "status": status_text,
                "available": connector_available,
                "power_kw": conn[2],  # power_kw
                "error": conn[4] if conn[4] and conn[4] != "NoError" else None
            })
        
        # Формируем ответ
        return {
            "success": True,
            "station_id": station_id,
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