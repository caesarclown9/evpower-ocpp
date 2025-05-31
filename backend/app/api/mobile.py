"""
📱 Mobile API endpoints для FlutterFlow
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.db.session import get_db
from ocpp_ws_server.redis_manager import redis_manager
from pydantic import BaseModel, Field

# ============================================================================
# ПЛАТЕЖНЫЕ ENDPOINTS O!DENGI
# ============================================================================

from app.schemas.ocpp import (
    BalanceTopupRequest, BalanceTopupResponse, 
    PaymentStatusResponse, PaymentWebhookData,
    ClientBalanceInfo, BalanceTopupInfo, PaymentTransactionInfo
)
from app.crud.ocpp_service import odengi_service, payment_service
from app.core.config import settings
from fastapi import BackgroundTasks

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
    """🔌 Начать зарядку с проверкой баланса и снятием средств"""
    try:
        # 1. Проверяем существование клиента и его баланс
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        client = client_check.fetchone()
        if not client:
            return {
                "success": False,
                "error": "client_not_found",
                "message": "Клиент не найден"
            }

        # 2. Проверяем станцию и получаем тариф
        station_check = db.execute(text("""
            SELECT s.id, s.status, s.price_per_kwh, tp.id as tariff_plan_id
            FROM stations s
            LEFT JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.id = :station_id AND s.status = 'active'
        """), {"station_id": request.station_id})
        
        station = station_check.fetchone()
        if not station:
            return {
                "success": False,
                "error": "station_unavailable",
                "message": "Станция недоступна"
            }
        
        # 3. Определяем тариф (из станции или тарифного плана)
        rate_per_kwh = float(station[2])  # price_per_kwh из станции
        
        if station[3]:  # Если есть тарифный план
            tariff_check = db.execute(text("""
                SELECT price FROM tariff_rules 
                WHERE tariff_plan_id = :tariff_plan_id 
                AND tariff_type = 'per_kwh' 
                AND is_active = true
                ORDER BY priority DESC LIMIT 1
            """), {"tariff_plan_id": station[3]})
            
            tariff_rule = tariff_check.fetchone()
            if tariff_rule:
                rate_per_kwh = float(tariff_rule[0])

        # 4. Рассчитываем стоимость зарядки
        estimated_cost = request.energy_kwh * rate_per_kwh
        
        # 5. Проверяем достаточность средств на балансе
        current_balance = Decimal(str(client[1]))
        if current_balance < Decimal(str(estimated_cost)):
            return {
                "success": False,
                "error": "insufficient_balance",
                "message": f"Недостаточно средств. Баланс: {current_balance} сом, требуется: {estimated_cost} сом",
                "current_balance": float(current_balance),
                "required_amount": estimated_cost,
                "missing_amount": estimated_cost - float(current_balance)
            }

        # 6. Проверяем коннектор
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
        
        # 7. Проверяем активные сессии клиента
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

        # 8. РЕЗЕРВИРУЕМ СРЕДСТВА НА БАЛАНСЕ
        new_balance = payment_service.update_client_balance(
            db, request.client_id, Decimal(str(estimated_cost)), "subtract",
            f"Резервирование средств для зарядки на станции {request.station_id}"
        )

        # 9. Создаем авторизацию OCPP
        id_tag = f"CLIENT_{request.client_id}"
        
        auth_check = db.execute(text("""
            SELECT id_tag FROM ocpp_authorization 
            WHERE id_tag = :id_tag
        """), {"id_tag": id_tag})
        
        if not auth_check.fetchone():
            db.execute(text("""
                INSERT INTO ocpp_authorization (id_tag, status, parent_id_tag, user_id) 
                VALUES (:id_tag, 'Accepted', NULL, :user_id)
            """), {"id_tag": id_tag, "user_id": request.client_id})

        # 10. Создаем сессию зарядки с резервированием средств
        session_insert = db.execute(text("""
            INSERT INTO charging_sessions 
            (user_id, station_id, start_time, status, limit_type, limit_value, amount)
            VALUES (:user_id, :station_id, :start_time, 'started', 'energy', :energy_kwh, :amount)
            RETURNING id
        """), {
            "user_id": request.client_id,
            "station_id": request.station_id,
            "start_time": datetime.now(timezone.utc),
            "energy_kwh": request.energy_kwh,
            "amount": estimated_cost
        })
        
        session_id = session_insert.fetchone()[0]

        # 11. Логируем транзакцию резервирования
        payment_service.create_payment_transaction(
            db, request.client_id, "balance_topup",
            -Decimal(str(estimated_cost)), current_balance, new_balance,
            f"Резервирование средств для сессии {session_id}",
            charging_session_id=session_id
        )

        # 12. Обновляем статус коннектора
        db.execute(text("""
            UPDATE connectors 
            SET status = 'occupied' 
            WHERE station_id = :station_id AND connector_number = :connector_id
        """), {"station_id": request.station_id, "connector_id": request.connector_id})

        # 13. Коммитим транзакцию
        db.commit()

        # 14. Проверяем подключение станции
        connected_stations = await redis_manager.get_stations()
        is_station_online = request.station_id in connected_stations
        
        if is_station_online:
            # Отправляем команду через Redis
            command_data = {
                "action": "RemoteStartTransaction",
                "connector_id": request.connector_id,
                "id_tag": id_tag,
                "session_id": session_id,
                "limit_type": 'energy',
                "limit_value": request.energy_kwh
            }
            
            await redis_manager.publish_command(request.station_id, command_data)
            
            logger.info(f"✅ Зарядка запущена: сессия {session_id}, средства зарезервированы {estimated_cost} сом")
            
            return {
                "success": True,
                "session_id": session_id,
                "station_id": request.station_id,
                "client_id": request.client_id,
                "connector_id": request.connector_id,
                "energy_kwh": request.energy_kwh,
                "rate_per_kwh": rate_per_kwh,
                "estimated_cost": estimated_cost,
                "reserved_amount": estimated_cost,
                "new_balance": float(new_balance),
                "message": "Зарядка запущена, средства зарезервированы",
                "station_online": True
            }
        else:
            logger.info(f"✅ Зарядка создана: сессия {session_id}, средства зарезервированы, станция оффлайн")
            
            return {
                "success": True,
                "session_id": session_id,
                "station_id": request.station_id,
                "client_id": request.client_id,
                "connector_id": request.connector_id,
                "energy_kwh": request.energy_kwh,
                "rate_per_kwh": rate_per_kwh,
                "estimated_cost": estimated_cost,
                "reserved_amount": estimated_cost,
                "new_balance": float(new_balance),
                "message": "Сессия создана, средства зарезервированы. Зарядка начнется при подключении станции.",
                "station_online": False
            }

    except ValueError as e:
        db.rollback()
        logger.error(f"Ошибка баланса при запуске зарядки: {e}")
        return {
            "success": False,
            "error": "balance_error",
            "message": str(e)
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
    """⏹️ Остановить зарядку с расчетом и возвратом средств"""
    try:
        # 1. Ищем активную сессию
        session_query = text("""
            SELECT cs.id, cs.user_id, cs.station_id, cs.start_time, cs.status, 
                   cs.limit_value, cs.amount, cs.energy, s.price_per_kwh,
                   tp.id as tariff_plan_id
            FROM charging_sessions cs
            LEFT JOIN stations s ON cs.station_id = s.id
            LEFT JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE cs.id = :session_id AND cs.status = 'started'
        """)
        
        session_result = db.execute(session_query, {"session_id": request.session_id})
        session = session_result.fetchone()
        
        if not session:
            return {
                "success": False,
                "error": "session_not_found",
                "message": "Активная сессия зарядки не найдена"
            }

        # 2. Получаем детали сессии
        session_id, user_id, station_id, start_time, status = session[:5]
        limit_value, reserved_amount, actual_energy, price_per_kwh = session[5:9]
        tariff_plan_id = session[9]
        
        # 3. Определяем актуальный тариф
        rate_per_kwh = float(price_per_kwh)
        
        if tariff_plan_id:
            tariff_check = db.execute(text("""
                SELECT price FROM tariff_rules 
                WHERE tariff_plan_id = :tariff_plan_id 
                AND tariff_type = 'per_kwh' 
                AND is_active = true
                ORDER BY priority DESC LIMIT 1
            """), {"tariff_plan_id": tariff_plan_id})
            
            tariff_rule = tariff_check.fetchone()
            if tariff_rule:
                rate_per_kwh = float(tariff_rule[0])

        # 4. Получаем фактическое потребление энергии
        actual_energy_consumed = float(actual_energy) if actual_energy else 0
        
        # Если энергия не записана в сессии, пытаемся получить из OCPP транзакций
        if actual_energy_consumed == 0:
            ocpp_energy_query = text("""
                SELECT COALESCE(ot.meter_stop - ot.meter_start, 0) as consumed_energy
                FROM ocpp_transactions ot
                WHERE ot.charging_session_id = :session_id
                ORDER BY ot.created_at DESC LIMIT 1
            """)
            
            ocpp_result = db.execute(ocpp_energy_query, {"session_id": session_id})
            ocpp_energy = ocpp_result.fetchone()
            
            if ocpp_energy and ocpp_energy[0]:
                actual_energy_consumed = float(ocpp_energy[0])

        # 5. Рассчитываем фактическую стоимость
        actual_cost = actual_energy_consumed * rate_per_kwh
        reserved_amount_decimal = Decimal(str(reserved_amount)) if reserved_amount else Decimal('0')
        actual_cost_decimal = Decimal(str(actual_cost))
        
        # 6. Рассчитываем возврат
        refund_amount = reserved_amount_decimal - actual_cost_decimal
        if refund_amount < 0:
            refund_amount = Decimal('0')

        # 7. Получаем текущий баланс клиента
        current_balance = payment_service.get_client_balance(db, user_id)

        # 8. Возвращаем неиспользованные средства
        if refund_amount > 0:
            new_balance = payment_service.update_client_balance(
                db, user_id, refund_amount, "add",
                f"Возврат неиспользованных средств за сессию {session_id}"
            )
            
            # Логируем транзакцию возврата
            payment_service.create_payment_transaction(
                db, user_id, "balance_topup",
                refund_amount,  # Положительная сумма для возврата
                current_balance, new_balance,
                f"Возврат за сессию {session_id}: потреблено {actual_energy_consumed} кВт⋅ч",
                charging_session_id=session_id
            )
        else:
            new_balance = current_balance

        # 9. Обновляем сессию зарядки
        update_session = text("""
            UPDATE charging_sessions 
            SET stop_time = NOW(), status = 'stopped', 
                energy = :actual_energy, amount = :actual_cost
            WHERE id = :session_id
        """)
        
        db.execute(update_session, {
            "actual_energy": actual_energy_consumed,
            "actual_cost": actual_cost,
            "session_id": session_id
        })

        # 10. Освобождаем коннектор
        connector_update = text("""
            UPDATE connectors 
            SET status = 'available' 
            WHERE station_id = :station_id
        """)
        db.execute(connector_update, {"station_id": station_id})

        # 11. Отправляем команду остановки через Redis
        connected_stations = await redis_manager.get_stations()
        is_station_online = station_id in connected_stations
        
        if is_station_online:
            # Получаем OCPP transaction_id
            ocpp_transaction_query = text("""
                SELECT transaction_id FROM ocpp_transactions 
                WHERE charging_session_id = :session_id 
                AND status = 'Started'
                ORDER BY created_at DESC LIMIT 1
            """)
            
            ocpp_result = db.execute(ocpp_transaction_query, {"session_id": session_id})
            ocpp_transaction = ocpp_result.fetchone()
            
            if ocpp_transaction:
                command_data = {
                    "action": "RemoteStopTransaction",
                    "transaction_id": ocpp_transaction[0]
                }
                
                await redis_manager.publish_command(station_id, command_data)

        # 12. Коммитим все изменения
        db.commit()

        logger.info(f"✅ Зарядка остановлена: сессия {session_id}, потреблено {actual_energy_consumed} кВт⋅ч, "
                   f"списано {actual_cost} сом, возвращено {refund_amount} сом")
        
        return {
            "success": True,
            "session_id": session_id,
            "station_id": station_id,
            "client_id": user_id,
            "start_time": start_time.isoformat() if start_time else None,
            "stop_time": datetime.now(timezone.utc).isoformat(),
            "energy_consumed": actual_energy_consumed,
            "rate_per_kwh": rate_per_kwh,
            "reserved_amount": float(reserved_amount_decimal),
            "actual_cost": actual_cost,
            "refund_amount": float(refund_amount),
            "new_balance": float(new_balance),
            "message": f"Зарядка завершена. Потреблено {actual_energy_consumed} кВт⋅ч, "
                      f"списано {actual_cost} сом, возвращено {refund_amount} сом",
            "station_online": is_station_online
        }

    except ValueError as e:
        db.rollback()
        logger.error(f"Ошибка баланса при остановке зарядки: {e}")
        return {
            "success": False,
            "error": "balance_error",
            "message": str(e)
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка остановки зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error", 
            "message": f"Ошибка: {str(e)}"
        }

@router.get("/charging/status/{session_id}")
async def get_charging_status(session_id: str, db: Session = Depends(get_db)):
    """📊 Проверить статус зарядки с полными данными из OCPP"""
    try:
        # Расширенный запрос с JOIN к OCPP транзакциям
        session_query = text("""
            SELECT 
                cs.id, cs.user_id, cs.station_id, cs.start_time, cs.stop_time,
                cs.energy, cs.amount, cs.status, cs.transaction_id,
                cs.limit_type, cs.limit_value,
                ot.transaction_id as ocpp_transaction_id,
                ot.meter_start, ot.meter_stop, ot.status as ocpp_status,
                s.price_per_kwh
            FROM charging_sessions cs
            LEFT JOIN ocpp_transactions ot ON cs.id = ot.charging_session_id 
                OR cs.transaction_id = CAST(ot.transaction_id AS TEXT)
            LEFT JOIN stations s ON cs.station_id = s.id
            WHERE cs.id = :session_id
        """)
        
        session_result = db.execute(session_query, {"session_id": session_id})
        session = session_result.fetchone()
        
        if not session:
            return {
                "success": False,
                "error": "session_not_found",
                "message": "Сессия зарядки не найдена"
            }
        
        # Получаем данные
        session_id = session[0]
        user_id = session[1]
        station_id = session[2]
        start_time = session[3]
        stop_time = session[4]
        energy_consumed = session[5] or 0
        amount_charged = session[6] or 0
        status = session[7]
        transaction_id = session[8]
        limit_type = session[9]
        limit_value = session[10] or 0
        ocpp_transaction_id = session[11]
        meter_start = session[12]
        meter_stop = session[13]
        ocpp_status = session[14]
        price_per_kwh = session[15] or 6.5
        
        # 🆕 УЛУЧШЕНИЕ: Расчет реальных данных из OCPP
        actual_energy_consumed = float(energy_consumed)
        actual_cost = float(amount_charged)
        
        # Если есть OCPP данные - используем их для более точного расчета
        if meter_start is not None and meter_stop is not None:
            # Рассчитываем из OCPP meter values
            ocpp_energy_wh = float(meter_stop) - float(meter_start)
            actual_energy_consumed = max(ocpp_energy_wh / 1000.0, actual_energy_consumed)  # Wh → kWh
            actual_cost = actual_energy_consumed * float(price_per_kwh)
        elif meter_start is not None and status == 'started':
            # Активная зарядка - получаем последние показания из meter_values
            latest_meter_query = text("""
                SELECT mv.value 
                FROM ocpp_meter_values mv
                JOIN ocpp_transactions ot ON mv.transaction_id = ot.id
                WHERE ot.charging_session_id = :session_id 
                AND mv.measurand = 'Energy.Active.Import.Register'
                ORDER BY mv.timestamp DESC LIMIT 1
            """)
            latest_result = db.execute(latest_meter_query, {"session_id": session_id})
            latest_meter = latest_result.fetchone()
            
            if latest_meter and latest_meter[0]:
                current_meter = float(latest_meter[0])
                ocpp_energy_wh = current_meter - float(meter_start)
                actual_energy_consumed = max(ocpp_energy_wh / 1000.0, actual_energy_consumed)
                actual_cost = actual_energy_consumed * float(price_per_kwh)
        
        # Рассчитываем прогресс
        progress_percent = 0
        if limit_type == "energy" and limit_value > 0:
            progress_percent = min(100, (actual_energy_consumed / float(limit_value)) * 100)
        elif limit_type == "amount" and limit_value > 0:
            progress_percent = min(100, (actual_cost / float(limit_value)) * 100)
        
        # Длительность в минутах
        duration_minutes = 0
        if start_time:
            end_time = stop_time or datetime.now(timezone.utc)
            duration_minutes = int((end_time - start_time).total_seconds() / 60)
        
        # 🆕 ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ: energy_consumed и cost как отдельные поля
        return {
            "success": True,
            "session_id": session_id,
            "status": status,
            "start_time": start_time.isoformat() if start_time else None,
            "stop_time": stop_time.isoformat() if stop_time else None,
            "duration_minutes": duration_minutes,
            
            # 🆕 Исправленные поля для совместимости с фронтендом
            "energy_consumed": round(actual_energy_consumed, 3),  # В кВт⋅ч
            "cost": round(actual_cost, 2),  # В сомах
            
            # Дублирующие поля для обратной совместимости
            "current_energy": round(actual_energy_consumed, 3),
            "current_amount": round(actual_cost, 2),
            
            # Лимиты и прогресс
            "limit_type": limit_type,
            "limit_value": round(float(limit_value), 2),
            "progress_percent": round(progress_percent, 1),
            
            # Метаданные
            "transaction_id": transaction_id,
            "ocpp_transaction_id": ocpp_transaction_id,
            "station_id": station_id,
            "client_id": user_id,
            "rate_per_kwh": float(price_per_kwh),
            
            # OCPP статус для отладки
            "ocpp_status": ocpp_status,
            "has_meter_data": meter_start is not None,
            
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

# ============================================================================
# ПЛАТЕЖНЫЕ ENDPOINTS O!DENGI
# ============================================================================

@router.post("/balance/topup", response_model=BalanceTopupResponse)
async def create_balance_topup(
    request: BalanceTopupRequest, 
    db: Session = Depends(get_db)
) -> BalanceTopupResponse:
    """💰 Создание платежа для пополнения баланса"""
    try:
        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        client = client_check.fetchone()
        if not client:
            return BalanceTopupResponse(
                success=False,
                error="client_not_found",
                client_id=request.client_id
            )

        # 2. Генерация безопасного order_id
        order_id = odengi_service.generate_secure_order_id("topup", request.client_id)
        
        # 3. Описание платежа
        description = f"Пополнение баланса клиента {request.client_id} на {request.amount} сом"
        
        # 4. Создание счета в O!Dengi
        amount_kopecks = int(request.amount * 100)
        odengi_response = await odengi_service.create_invoice(
            order_id=order_id,
            description=description,
            amount_kopecks=amount_kopecks
        )
        
        if not odengi_response.get("invoice_id"):
            return BalanceTopupResponse(
                success=False,
                error="odengi_error",
                client_id=request.client_id
            )

        # 5. Сохранение в базу данных
        topup_insert = db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, qr_code_url, app_link, status, odengi_status)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, :qr_code_url, :app_link, 'pending', 0)
            RETURNING id
        """), {
            "invoice_id": odengi_response["invoice_id"],
            "order_id": order_id,
            "merchant_id": settings.ODENGI_MERCHANT_ID,
            "client_id": request.client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_code_url": odengi_response.get("qr"),
            "app_link": odengi_response.get("link_app")
        })
        
        db.commit()
        
        logger.info(f"Пополнение создано: {order_id}, invoice_id: {odengi_response['invoice_id']}")
        
        return BalanceTopupResponse(
            success=True,
            invoice_id=odengi_response["invoice_id"],
            order_id=order_id,
            qr_code=odengi_response.get("qr"),
            app_link=odengi_response.get("link_app"),
            amount=request.amount,
            client_id=request.client_id,
            current_balance=float(client[1])  # Текущий баланс
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка создания пополнения: {e}")
        return BalanceTopupResponse(
            success=False,
            error=f"internal_error: {str(e)}",
            client_id=request.client_id
        )

@router.get("/payment/status/{invoice_id}", response_model=PaymentStatusResponse)
async def get_payment_status(
    invoice_id: str,
    db: Session = Depends(get_db)
) -> PaymentStatusResponse:
    """📊 Проверка статуса платежа (пополнение или зарядка)"""
    try:
        # 1. Ищем платеж в таблице пополнений баланса
        topup_check = db.execute(text("""
            SELECT id, invoice_id, order_id, client_id, requested_amount, status, odengi_status
            FROM balance_topups WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        topup = topup_check.fetchone()
        
        if not topup:
            return PaymentStatusResponse(
                success=False,
                status=0,
                status_text="Платеж не найден",
                error="payment_not_found"
            )

        # 2. Запрос статуса из O!Dengi
        odengi_response = await odengi_service.get_payment_status(
            invoice_id=invoice_id,
            order_id=topup[2]  # order_id
        )
        
        odengi_status = odengi_response.get('status', 0)
        paid_amount = odengi_response.get('amount', 0) / 100 if odengi_response.get('amount') else None
        
        # 3. Обновление локального статуса
        status_mapping = {
            0: "pending",
            1: "paid",
            2: "cancelled", 
            3: "refunded",
            4: "partial_refund"
        }
        
        new_status = status_mapping.get(odengi_status, "pending")
        
        # Обновляем статус в базе
        if topup[5] != new_status:  # Если статус изменился
            db.execute(text("""
                UPDATE balance_topups 
                SET status = :new_status, odengi_status = :odengi_status,
                    paid_amount = :paid_amount, updated_at = NOW()
                WHERE invoice_id = :invoice_id
            """), {
                "new_status": new_status,
                "odengi_status": odengi_status,
                "paid_amount": paid_amount,
                "invoice_id": invoice_id
            })
            
            db.commit()
        
        # 4. Определение возможности операций
        can_proceed = odengi_service.can_proceed(odengi_status)
        
        logger.info(f"Статус платежа {invoice_id}: {new_status} (O!Dengi: {odengi_status})")
        
        return PaymentStatusResponse(
            success=True,
            status=odengi_status,
            status_text=odengi_service.get_status_text(odengi_status),
            amount=float(topup[4]),  # requested_amount
            paid_amount=paid_amount,
            invoice_id=invoice_id,
            can_proceed=can_proceed,
            can_start_charging=False  # Пополнение баланса не дает прямого доступа к зарядке
        )
        
    except Exception as e:
        logger.error(f"Ошибка проверки статуса платежа {invoice_id}: {e}")
        return PaymentStatusResponse(
            success=False,
            status=0,
            status_text="Ошибка проверки статуса",
            error=f"internal_error: {str(e)}"
        )

@router.post("/payment/webhook")
async def handle_payment_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """🔔 Обработка webhook уведомлений от O!Dengi"""
    try:
        # 1. Получение сырых данных и подписи
        payload = await request.body()
        webhook_signature = request.headers.get('X-O-Dengi-Signature', '')
        
        # 2. Верификация подписи
        if not odengi_service.verify_webhook_signature(payload, webhook_signature):
            logger.warning(f"Invalid webhook signature from {request.client.host}")
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        # 3. Парсинг JSON данных
        webhook_data = PaymentWebhookData.parse_raw(payload)
        
        # 4. Валидация order_id
        if not odengi_service.validate_order_id(webhook_data.order_id):
            logger.warning(f"Invalid order_id in webhook: {webhook_data.order_id}")
            raise HTTPException(status_code=400, detail="Invalid order_id")
        
        # 5. Поиск платежа в базе
        topup_check = db.execute(text("""
            SELECT id, client_id, requested_amount, status FROM balance_topups 
            WHERE invoice_id = :invoice_id
        """), {"invoice_id": webhook_data.invoice_id})
        
        topup = topup_check.fetchone()
        
        if not topup:
            logger.warning(f"Payment not found for webhook: {webhook_data.invoice_id}")
            return {"status": "payment_not_found"}
        
        # 6. Обработка пополнения баланса
        if topup and webhook_data.status == 1:  # Оплачено
            background_tasks.add_task(
                process_balance_topup,
                topup[0],  # topup_id
                topup[1],  # client_id
                webhook_data.paid_amount / 100 if webhook_data.paid_amount else topup[2],  # amount
                webhook_data.invoice_id
            )
        
        return {"status": "received", "invoice_id": webhook_data.invoice_id}
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Webhook processing failed")

@router.get("/balance/{client_id}", response_model=ClientBalanceInfo)
async def get_client_balance(client_id: str, db: Session = Depends(get_db)) -> ClientBalanceInfo:
    """💰 Получение информации о балансе клиента"""
    try:
        # Получаем информацию о клиенте и балансе
        client_info = db.execute(text("""
            SELECT id, balance, updated_at FROM clients WHERE id = :client_id
        """), {"client_id": client_id})
        
        client = client_info.fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Клиент не найден")
        
        # Получаем дату последнего пополнения
        last_topup = db.execute(text("""
            SELECT paid_at FROM balance_topups 
            WHERE client_id = :client_id AND status = 'paid'
            ORDER BY paid_at DESC LIMIT 1
        """), {"client_id": client_id})
        
        last_topup_date = last_topup.fetchone()
        
        # Подсчитываем общую потраченную сумму
        total_spent = db.execute(text("""
            SELECT COALESCE(SUM(amount), 0) FROM payment_transactions_odengi 
            WHERE client_id = :client_id AND transaction_type = 'charging_payment'
        """), {"client_id": client_id})
        
        spent_amount = total_spent.fetchone()[0]
        
        return ClientBalanceInfo(
            client_id=client_id,
            balance=float(client[1]),
            currency=settings.DEFAULT_CURRENCY,
            last_topup_at=last_topup_date[0] if last_topup_date else None,
            total_spent=float(spent_amount) if spent_amount else 0
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка получения баланса клиента {client_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка получения баланса")

# ============================================================================
# BACKGROUND TASKS ДЛЯ ОБРАБОТКИ ПЛАТЕЖЕЙ
# ============================================================================

async def process_balance_topup(topup_id: int, client_id: str, amount: float, invoice_id: str):
    """Обработка успешного пополнения баланса"""
    try:
        with next(get_db()) as db:
            # Получаем текущий баланс
            current_balance = payment_service.get_client_balance(db, client_id)
            
            # Пополняем баланс
            new_balance = payment_service.update_client_balance(
                db, client_id, Decimal(str(amount)), "add",
                f"Пополнение баланса через O!Dengi (invoice: {invoice_id})"
            )
            
            # Создаем транзакцию
            payment_service.create_payment_transaction(
                db, client_id, "balance_topup", 
                Decimal(str(amount)), current_balance, new_balance,
                f"Пополнение баланса через O!Dengi",
                balance_topup_id=topup_id
            )
            
            # Обновляем статус пополнения
            db.execute(text("""
                UPDATE balance_topups 
                SET status = 'paid', paid_at = NOW(), paid_amount = :amount
                WHERE id = :topup_id
            """), {"amount": amount, "topup_id": topup_id})
            
            db.commit()
            
            logger.info(f"✅ Баланс пополнен: клиент {client_id}, сумма {amount}, новый баланс {new_balance}")
            
    except Exception as e:
        logger.error(f"Ошибка обработки пополнения баланса: {e}") 