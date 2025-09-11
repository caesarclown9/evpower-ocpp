"""
📱 Mobile API endpoints для FlutterFlow
"""
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.db.session import get_db
from ocpp_ws_server.redis_manager import redis_manager
from pydantic import BaseModel, Field, validator

from app.services.payment_provider_service import get_payment_provider_service, get_qr_payment_service, get_card_payment_service
from app.services.obank_service import obank_service
# Аутентификация убрана - client_id передается напрямую из FlutterFlow

# ============================================================================
# ПЛАТЕЖНЫЕ ENDPOINTS O!DENGI
# ============================================================================

from app.schemas.ocpp import (
    BalanceTopupRequest, BalanceTopupResponse, 
    PaymentStatusResponse, PaymentWebhookData,
    ClientBalanceInfo, BalanceTopupInfo, PaymentTransactionInfo,
    H2HPaymentRequest, H2HPaymentResponse,
    TokenPaymentRequest, TokenPaymentResponse,
    CreateTokenRequest, CreateTokenResponse
)
from app.crud.ocpp_service import payment_service, payment_lifecycle_service
from app.core.config import settings

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
    energy_kwh: Optional[float] = Field(None, gt=0, le=200, description="Энергия для зарядки в кВт⋅ч")
    amount_som: Optional[float] = Field(None, gt=0, description="Предоплаченная сумма в сомах")
    
    @validator('amount_som', 'energy_kwh')
    def validate_limits(cls, v, values):
        """Валидация лимитов зарядки"""
        # Поддерживаем 3 режима:
        # 1. energy_kwh + amount_som - лимит по энергии с максимальной суммой
        # 2. Только amount_som - лимит по сумме
        # 3. Ничего не указано - полностью безлимитная зарядка
        return v

class ChargingStopRequest(BaseModel):
    """⏹️ Запрос на остановку зарядки"""
    session_id: str = Field(..., min_length=1, description="ID сессии зарядки")

# ================== API Endpoints ==================

@router.post("/charging/start")
async def start_charging(
    request: ChargingStartRequest, 
    db: Session = Depends(get_db)
):
    """🔌 Начать зарядку с проверкой баланса и снятием средств"""
    # Логируем запрос
    logger.info(f"Starting charging: client_id={request.client_id}, station_id={request.station_id}")
    
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
        
        # 3. Определяем тариф: ПРИОРИТЕТ СТАНЦИИ над тарифным планом
        rate_per_kwh = 13.5  # fallback по умолчанию
        
        # Сначала проверяем тариф станции
        if station[2]:  # Если у станции есть price_per_kwh
            rate_per_kwh = float(station[2])
        elif station[3]:  # Только если у станции НЕТ тарифа - ищем в тарифном плане
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

        # 4. Рассчитываем стоимость зарядки с финансовой защитой
        current_balance = Decimal(str(client[1]))
        
        if request.energy_kwh and request.amount_som:
            # РЕЖИМ 1: Лимит по энергии + максимальная сумма
            estimated_cost = request.energy_kwh * rate_per_kwh
            reservation_amount = min(estimated_cost, request.amount_som)
            
        elif request.amount_som:
            # РЕЖИМ 2: Лимит только по сумме
            max_allowed_amount = min(float(current_balance), request.amount_som)
            
            if request.amount_som > float(current_balance):
                return {
                    "success": False,
                    "error": "amount_exceeds_balance",
                    "message": f"Указанная сумма ({request.amount_som} сом) превышает баланс ({current_balance} сом)",
                    "current_balance": float(current_balance),
                    "max_allowed_amount": float(current_balance),
                    "requested_amount": request.amount_som
                }
            
            estimated_cost = 0  # Будет рассчитана по факту
            reservation_amount = max_allowed_amount
            
        elif request.energy_kwh:
            # РЕЖИМ 3: Лимит только по энергии (резервируем расчетную стоимость)
            estimated_cost = request.energy_kwh * rate_per_kwh
            reservation_amount = estimated_cost
            
        else:
            # РЕЖИМ 4: 🚀 ПОЛНОСТЬЮ БЕЗЛИМИТНАЯ ЗАРЯДКА
            # 🆕 ИСПРАВЛЕНИЕ: Резервируем 200 сом или весь баланс если он меньше
            estimated_cost = 0  # Будет рассчитана по факту
            max_reservation = 200.0  # Максимальный резерв для безлимитной зарядки
            reservation_amount = min(float(current_balance), max_reservation)
            
            if current_balance <= 0:
                return {
                    "success": False,
                    "error": "zero_balance",
                    "message": "Недостаточно средств для безлимитной зарядки",
                    "current_balance": float(current_balance)
                }
            
            # Дополнительная проверка минимального резерва
            min_reservation = 10.0  # Минимум 10 сом для старта
            if reservation_amount < min_reservation:
                return {
                    "success": False,
                    "error": "insufficient_balance",
                    "message": f"Минимальный резерв для безлимитной зарядки: {min_reservation} сом. Баланс: {current_balance} сом",
                    "current_balance": float(current_balance),
                    "required_amount": min_reservation
                }
        
        # 5. Проверяем достаточность средств на балансе
        if current_balance < Decimal(str(reservation_amount)):
            return {
                "success": False,
                "error": "insufficient_balance",
                "message": f"Недостаточно средств. Баланс: {current_balance} сом, требуется: {reservation_amount} сом",
                "current_balance": float(current_balance),
                "required_amount": reservation_amount,
                "missing_amount": reservation_amount - float(current_balance)
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
        
        # 7. Проверяем, нет ли уже активной сессии для клиента
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
            db, request.client_id, Decimal(str(reservation_amount)), "subtract",
            f"Резервирование средств для зарядки на станции {request.station_id}"
        )

        # 9. Создаем ocpp_sessions запись с правильным idTag
        # 🆕 ИСПРАВЛЕНИЕ: Используем номер телефона вместо CLIENT_ префикса
        phone_query = text("""
            SELECT phone FROM clients WHERE id = :client_id
        """)
        phone_result = db.execute(phone_query, {"client_id": request.client_id}).fetchone()
        id_tag = phone_result[0] if phone_result else f"CLIENT_{request.client_id}"
        
        auth_check = db.execute(text("""
            SELECT id_tag FROM ocpp_authorization 
            WHERE id_tag = :id_tag
        """), {"id_tag": id_tag})
        
        if not auth_check.fetchone():
            db.execute(text("""
                INSERT INTO ocpp_authorization (id_tag, status, parent_id_tag, client_id) 
                VALUES (:id_tag, 'Accepted', NULL, :client_id)
            """), {"id_tag": id_tag, "client_id": request.client_id})

        # 10. Создаем сессию зарядки с резервированием средств
        # 🔧 ЛОГИКА ЛИМИТОВ для базы данных
        if request.energy_kwh and request.amount_som:
            # РЕЖИМ 1: Энергия + сумма
            limit_type = 'energy'
            limit_value = request.energy_kwh
        elif request.amount_som:
            # РЕЖИМ 2: Только сумма
            limit_type = 'amount' 
            limit_value = request.amount_som
        elif request.energy_kwh:
            # РЕЖИМ 3: Только энергия
            limit_type = 'energy'
            limit_value = request.energy_kwh
        else:
            # РЕЖИМ 4: Полностью безлимитная
            limit_type = 'none'
            limit_value = 0
        
        session_insert = db.execute(text("""
            INSERT INTO charging_sessions 
            (user_id, station_id, start_time, status, limit_type, limit_value, amount)
            VALUES (:user_id, :station_id, :start_time, 'started', :limit_type, :limit_value, :amount)
            RETURNING id
        """), {
            "user_id": request.client_id,
            "station_id": request.station_id,
            "start_time": datetime.now(timezone.utc),
            "limit_type": limit_type,
            "limit_value": limit_value,
            "amount": reservation_amount
        })
        
        session_id = session_insert.fetchone()[0]

        # 11. Логируем транзакцию резервирования
        payment_service.create_payment_transaction(
            db, request.client_id, "charge_reserve",
            -Decimal(str(reservation_amount)), current_balance, new_balance,
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
                "session_id": session_id
            }
            
            # Добавляем лимиты в Redis команду
            command_data["limit_type"] = limit_type
            command_data["limit_value"] = limit_value
            
            await redis_manager.publish_command(request.station_id, command_data)
            
            logger.info(f"✅ Зарядка запущена: сессия {session_id}, средства зарезервированы {reservation_amount} сом")
            
            return {
                "success": True,
                "session_id": session_id,
                "station_id": request.station_id,
                "client_id": request.client_id,
                "connector_id": request.connector_id,
                "energy_kwh": request.energy_kwh,
                "rate_per_kwh": rate_per_kwh,
                "estimated_cost": reservation_amount,
                "reserved_amount": reservation_amount,
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
                "estimated_cost": reservation_amount,
                "reserved_amount": reservation_amount,
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
            "message": "Ошибка получения баланса"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при запуске зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "message": "Внутренняя ошибка сервера"
        }

@router.post("/charging/stop")
async def stop_charging(
    request: ChargingStopRequest, 
    db: Session = Depends(get_db)
):
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
        
        # 🆕 НОВАЯ ЛОГИКА: Дополнительное списание при превышении резерва
        additional_charge = Decimal('0')
        if actual_cost_decimal > reserved_amount_decimal:
            # Фактическая стоимость превышает резерв - нужно доплатить
            additional_charge = actual_cost_decimal - reserved_amount_decimal
            
            # Проверяем достаточность средств для дополнительного списания
            current_balance = payment_service.get_client_balance(db, user_id)
            if current_balance < additional_charge:
                # Недостаточно средств - ограничиваем списание доступным балансом
                logger.warning(f"⚠️ НЕДОСТАТОК СРЕДСТВ для доплаты в сессии {session_id}: "
                              f"требуется {additional_charge}, доступно {current_balance}. "
                              f"Ограничиваем списание.")
                additional_charge = current_balance
                actual_cost_decimal = reserved_amount_decimal + additional_charge
                actual_cost = float(actual_cost_decimal)
            else:
                # Средств достаточно - списываем дополнительную сумму
                payment_service.update_client_balance(
                    db, user_id, additional_charge, "subtract",
                    f"Дополнительное списание за превышение резерва в сессии {session_id}"
                )
                
                # Логируем дополнительную транзакцию
                balance_after_additional = payment_service.get_client_balance(db, user_id)
                payment_service.create_payment_transaction(
                    db, user_id, "charge_payment",
                    -additional_charge,  # Отрицательная сумма для списания
                    current_balance, balance_after_additional,
                    f"Доплата за сессию {session_id}: превышение резерва на {additional_charge} сом",
                    charging_session_id=session_id
                )
                
                logger.info(f"💳 ДОПОЛНИТЕЛЬНОЕ СПИСАНИЕ в сессии {session_id}: "
                           f"{additional_charge} сом (превышение резерва)")
        
        # 6. Рассчитываем возврат (только если нет дополнительного списания)
        if additional_charge > 0:
            refund_amount = Decimal('0')  # Нет возврата при доплате
        else:
            refund_amount = reserved_amount_decimal - actual_cost_decimal
            if refund_amount < 0:
                refund_amount = Decimal('0')

        # 7. Получаем актуальный баланс для возврата
        current_balance = payment_service.get_client_balance(db, user_id)

        # 8. Возвращаем неиспользованные средства
        if refund_amount > 0:
            new_balance = payment_service.update_client_balance(
                db, user_id, refund_amount, "add",
                f"Возврат неиспользованных средств за сессию {session_id}"
            )
            
            # Логируем транзакцию возврата
            payment_service.create_payment_transaction(
                db, user_id, "charge_refund",
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
            "message": "Ошибка получения баланса"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка остановки зарядки: {e}")
        return {
            "success": False,
            "error": "internal_error", 
            "message": "Внутренняя ошибка сервера"
        }

@router.get("/charging/status/{session_id}")
async def get_charging_status(
    session_id: str, 
    db: Session = Depends(get_db)
):
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
        price_per_kwh = session[15] or 13.5
        
        # 🆕 УЛУЧШЕНИЕ: Расчет реальных данных из OCPP
        actual_energy_consumed = float(energy_consumed)
        actual_cost = actual_energy_consumed * float(price_per_kwh)
        
        # Если есть OCPP данные - используем их для более точного расчета
        if meter_start is not None and meter_stop is not None:
            # Рассчитываем из OCPP meter values
            ocpp_energy_wh = float(meter_stop) - float(meter_start)
            actual_energy_consumed = max(ocpp_energy_wh / 1000.0, actual_energy_consumed)  # Wh → kWh
            actual_cost = actual_energy_consumed * float(price_per_kwh)
        elif meter_start is not None and status == 'started':
            # Активная зарядка - получаем последние показания из meter_values
            latest_meter_query = text("""
                SELECT mv.energy_active_import_register
                FROM ocpp_meter_values mv
                JOIN ocpp_transactions ot ON mv.ocpp_transaction_id = ot.transaction_id
                WHERE ot.charging_session_id = :session_id
                AND mv.energy_active_import_register IS NOT NULL
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
        
        # 🆕 ПОЛУЧЕНИЕ РАСШИРЕННЫХ ДАННЫХ ИЗ METER VALUES
        latest_meter_data = {}
        meter_current = None
        last_update = None
        
        if ocpp_transaction_id:
            # Получаем последние показания всех датчиков
            latest_meter_query = text("""
                SELECT 
                    energy_active_import_register,
                    power_active_import,
                    current_import,
                    voltage,
                    temperature,
                    soc,
                    timestamp,
                    sampled_values
                FROM ocpp_meter_values 
                WHERE ocpp_transaction_id = :transaction_id
                ORDER BY timestamp DESC 
                LIMIT 1
            """)
            
            latest_result = db.execute(latest_meter_query, {"transaction_id": ocpp_transaction_id})
            latest_meter = latest_result.fetchone()
            
            if latest_meter:
                latest_meter_data = {
                    'energy_register': latest_meter[0],
                    'power': latest_meter[1], 
                    'current': latest_meter[2],
                    'voltage': latest_meter[3],
                    'temperature': latest_meter[4],
                    'soc': latest_meter[5],
                    'timestamp': latest_meter[6],
                    'sampled_values': latest_meter[7]
                }
                meter_current = float(latest_meter[0]) if latest_meter[0] else None
                last_update = latest_meter[6].isoformat() if latest_meter[6] else None
        
        # 🔍 ПРОВЕРКА СТАТУСА СТАНЦИИ ОНЛАЙН
        station_online = False
        try:
            connected_stations = await redis_manager.get_stations()
            station_online = station_id in connected_stations
        except Exception as e:
            logger.warning(f"Не удалось проверить статус станции {station_id}: {e}")
        
        # 🛡️ БЕЗОПАСНАЯ ОБРАБОТКА ДАННЫХ С NULL ПРОВЕРКАМИ
        def safe_float(value, default=0.0):
            """Безопасное преобразование в float с обработкой None"""
            if value is None:
                return default
            try:
                return float(value)
            except (ValueError, TypeError):
                return default
        
        def safe_int(value, default=0):
            """Безопасное преобразование в int с обработкой None"""
            if value is None:
                return default
            try:
                return int(value)
            except (ValueError, TypeError):
                return default
        
        # 🔌 ПАРАМЕТРЫ ЗАРЯДКИ (из latest meter data)
        charging_power = safe_float(latest_meter_data.get('power'), 0.0) / 1000.0  # W → kW
        station_current = safe_float(latest_meter_data.get('current'), 0.0)
        station_voltage = safe_float(latest_meter_data.get('voltage'), 0.0)
        
        # 🚗 ДАННЫЕ ЭЛЕКТРОМОБИЛЯ  
        ev_battery_soc = safe_int(latest_meter_data.get('soc'), 0)
        
        # Парсим дополнительные данные из sampled_values JSON
        ev_current = 0.0
        ev_voltage = 0.0
        station_body_temp = 0
        station_outlet_temp = 0
        station_inlet_temp = 0
        
        if latest_meter_data.get('sampled_values'):
            try:
                sampled_values = latest_meter_data['sampled_values']
                if isinstance(sampled_values, list):
                    for sample in sampled_values:
                        measurand = sample.get('measurand', '')
                        value = safe_float(sample.get('value'), 0.0)
                        
                        # Дополнительные measurand для ЭМ и температур
                        if measurand == 'Current.Export':  # Ток от ЭМ
                            ev_current = value
                        elif measurand == 'Voltage.Export':  # Напряжение от ЭМ  
                            ev_voltage = value
                        elif measurand == 'Temperature.Outlet':  # Температура разъема
                            station_outlet_temp = safe_int(value, 0)
                        elif measurand == 'Temperature.Inlet':  # Температура входа
                            station_inlet_temp = safe_int(value, 0)
                        elif measurand == 'Temperature':  # Общая температура корпуса
                            station_body_temp = safe_int(value, 0)
            except Exception as e:
                logger.warning(f"Ошибка парсинга sampled_values: {e}")
        
        # Если температура корпуса не указана отдельно, используем основную
        if station_body_temp == 0:
            station_body_temp = safe_int(latest_meter_data.get('temperature'), 0)
        
        # 📊 ПОКАЗАНИЯ СЧЕТЧИКА
        meter_start_wh = safe_float(meter_start, 0.0)
        meter_current_wh = meter_current or meter_start_wh
        
        # 🆕 РАСШИРЕННЫЙ ОТВЕТ API
        return {
            "success": True,
            "session_id": session_id,
            "status": status,
            "start_time": start_time.isoformat() if start_time else None,
            "stop_time": stop_time.isoformat() if stop_time else None,
            "duration_minutes": duration_minutes,
            
            # ⚡ ЭНЕРГЕТИЧЕСКИЕ ДАННЫЕ
            "energy_consumed": round(actual_energy_consumed, 3),  # кВт⋅ч
            "energy_consumed_kwh": round(actual_energy_consumed, 3),  # кВт⋅ч (для совместимости)
            "cost": round(actual_cost, 2),  # сом
            "final_amount_som": round(actual_cost, 2),  # сом (для совместимости)
            "amount_charged_som": round(actual_cost, 2),  # сом (для совместимости)
            "limit_value": round(float(limit_value), 2),  # лимит
            "progress_percent": round(progress_percent, 1),  # % выполнения
            
            # 🔌 ПАРАМЕТРЫ ЗАРЯДКИ (реальные данные от станции)
            "charging_power": round(charging_power, 1),  # кВт
            "station_current": round(station_current, 1),  # А
            "station_voltage": round(station_voltage, 1),  # В
            
            # 🚗 ДАННЫЕ ЭЛЕКТРОМОБИЛЯ
            "ev_battery_soc": ev_battery_soc,  # %
            "ev_current": round(ev_current, 1),  # А
            "ev_voltage": round(ev_voltage, 1),  # В
            
            # 🌡️ ТЕМПЕРАТУРНЫЙ МОНИТОРИНГ
            "temperatures": {
                "station_body": station_body_temp,  # °C
                "station_outlet": station_outlet_temp,  # °C  
                "station_inlet": station_inlet_temp  # °C
            },
            
            # 📊 ТЕХНИЧЕСКИЕ ДАННЫЕ
            "meter_start": int(meter_start_wh),  # Wh
            "meter_current": int(meter_current_wh),  # Wh
            "station_online": station_online,
            "last_update": last_update,
            
            # 🔄 ОБРАТНАЯ СОВМЕСТИМОСТЬ
            "current_energy": round(actual_energy_consumed, 3),
            "current_amount": round(actual_cost, 2),
            "limit_type": limit_type,
            "transaction_id": transaction_id,
            "ocpp_transaction_id": ocpp_transaction_id,
            "station_id": station_id,
            "client_id": user_id,
            "rate_per_kwh": float(price_per_kwh),
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
            "message": "Внутренняя ошибка сервера"
        }

@router.get("/station/status/{station_id}") 
async def get_station_status(
    station_id: str, 
    db: Session = Depends(get_db)
):
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
        
        # Логируем для отладки
        connector_rows = connectors_result.fetchall()
        logger.info(f"Station {station_id}: найдено {len(connector_rows)} коннекторов")
        
        for conn in connector_rows:
            connector_status = conn[3]  # status
            logger.info(f"Коннектор {conn[0]}: тип={conn[1]}, мощность={conn[2]}, статус={connector_status}")
            
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
        
        logger.info(f"Обработано коннекторов: {len(connectors)}, доступных: {available_count}, занятых: {occupied_count}")
        
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
            "tariff_rub_kwh": float(station_data[8]) if station_data[8] else 13.5,
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
            "message": "Внутренняя ошибка сервера"
        }

# ============================================================================
# ПЛАТЕЖНЫЕ STATUS CHECK И WEBHOOK ENDPOINTS  
# ============================================================================

@router.get("/payment/status/{invoice_id}", response_model=PaymentStatusResponse)
async def get_payment_status(
    invoice_id: str,
    db: Session = Depends(get_db)
) -> PaymentStatusResponse:
    """📊 Проверка статуса платежа с учетом времени жизни"""
    try:
        # 1. Ищем платеж в таблице пополнений баланса
        topup_check = db.execute(text("""
            SELECT id, invoice_id, order_id, client_id, requested_amount, status, odengi_status,
                   qr_expires_at, invoice_expires_at, last_status_check_at, created_at
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

        # 2. 🕐 Проверяем время жизни
        qr_expires_at = topup[7]
        invoice_expires_at = topup[8]
        qr_expired = payment_lifecycle_service.is_qr_expired(qr_expires_at)
        invoice_expired = payment_lifecycle_service.is_invoice_expired(invoice_expires_at)
        
        # Если invoice истек - автоматически отменяем
        if invoice_expired and topup[5] == "processing":
            db.execute(text("""
                UPDATE balance_topups 
                SET status = 'canceled', completed_at = NOW(), needs_status_check = false
                WHERE invoice_id = :invoice_id
            """), {"invoice_id": invoice_id})
            db.commit()
            
            return PaymentStatusResponse(
                success=True,
                status=2,  # canceled
                status_text="Платеж отменен - время истекло",
                amount=float(topup[4]),
                invoice_id=invoice_id,
                qr_expired=True,
                invoice_expired=True,
                qr_expires_at=qr_expires_at,
                invoice_expires_at=invoice_expires_at
            )

        # 3. Читаем актуальный статус из базы данных (обновленный background task)
        fresh_topup_check = db.execute(text("""
            SELECT status, odengi_status, paid_amount, last_status_check_at
            FROM balance_topups WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        fresh_topup = fresh_topup_check.fetchone()
        if fresh_topup:
            db_status, db_odengi_status, db_paid_amount, db_last_check = fresh_topup
        else:
            db_status, db_odengi_status, db_paid_amount, db_last_check = topup[5], topup[6], None, topup[9]
        
        # 4. Маппинг статусов из базы данных
        status_mapping = {
            "processing": 0,
            "approved": 1,
            "canceled": 2, 
            "refunded": 3,
            "partial_refund": 4
        }
        
        numeric_status = status_mapping.get(db_status, 0)
        
        # 5. Определение возможности операций и нужны ли callback проверки
        can_proceed = (numeric_status == 1)  # Только для approved платежей
        needs_callback_check = (db_status == "processing" and 
                               not invoice_expired and 
                               payment_lifecycle_service.should_status_check(
                                   topup[10], db_last_check, 0, db_status))  # created_at, last_check_at
        
        # Текст статуса
        status_texts = {
            0: "В обработке",
            1: "Оплачено", 
            2: "Отменен",
            3: "Возвращен",
            4: "Частичный возврат"
        }
        status_text = status_texts.get(numeric_status, "Неизвестный статус")
        
        logger.info(f"🕐 Статус платежа {invoice_id}: {db_status} (numeric: {numeric_status}), QR истек: {qr_expired}, Invoice истек: {invoice_expired}")
        
        return PaymentStatusResponse(
            success=True,
            status=numeric_status,
            status_text=status_text,
            amount=float(topup[4]),  # requested_amount
            paid_amount=float(db_paid_amount) if db_paid_amount else None,
            invoice_id=invoice_id,
            can_proceed=can_proceed,
            can_start_charging=False,
            qr_expired=qr_expired,
            invoice_expired=invoice_expired,
            qr_expires_at=qr_expires_at,
            invoice_expires_at=invoice_expires_at,
            last_status_check_at=db_last_check,
            needs_callback_check=needs_callback_check
        )
        
    except Exception as e:
        logger.error(f"Ошибка проверки статуса платежа {invoice_id}: {e}")
        return PaymentStatusResponse(
            success=False,
            status=0,
            status_text="Ошибка проверки статуса",
            error="internal_error"
        )

@router.post("/payment/status-check/{invoice_id}")
async def force_payment_status_check(
    invoice_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """🔍 Принудительная проверка статуса платежа через O!Dengi API"""
    try:
        # 1. Проверяем существование платежа
        payment_check = db.execute(text("""
            SELECT 'balance_topups' as table_name, invoice_id, status, created_at, invoice_expires_at
            FROM balance_topups WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        payment = payment_check.fetchone()
        if not payment:
            return {
                "success": False,
                "error": "payment_not_found",
                "message": "Платеж не найден"
            }
        
        table_name, _, status, created_at, invoice_expires_at = payment
        
        # 2. Проверяем время жизни
        if payment_lifecycle_service.is_invoice_expired(invoice_expires_at):
            return {
                "success": False,
                "error": "payment_expired",
                "message": "Платеж истек, проверка невозможна",
                "invoice_expires_at": invoice_expires_at.isoformat()
            }
        
        # 3. Проверяем статус (не проверяем завершенные)
        if status in ['approved', 'canceled', 'refunded']:
            return {
                "success": False,
                "error": "payment_completed",
                "message": f"Платеж уже завершен со статусом: {status}",
                "current_status": status
            }
        
        # 4. Запускаем проверку в фоне
        background_tasks.add_task(
            payment_lifecycle_service.perform_status_check,
            db, table_name, invoice_id
        )
        
        logger.info(f"🔍 Запущена принудительная проверка статуса для {invoice_id}")
        
        return {
            "success": True,
            "message": "Проверка статуса запущена",
            "invoice_id": invoice_id,
            "check_type": "manual",
            "estimated_completion_seconds": 5
        }
        
    except Exception as e:
        logger.error(f"Ошибка запуска проверки статуса {invoice_id}: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка запуска проверки: {str(e)}"
        }

@router.post("/payment/webhook")
async def handle_payment_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """🔔 Обработка webhook уведомлений от платежных провайдеров"""
    try:
        # 1. Получение сырых данных и подписи
        payload = await request.body()
        
        # 2. Определяем провайдера и верифицируем подпись
        provider_name = get_payment_provider_service().get_provider_name()
        
        if provider_name == "OBANK":
            # OBANK использует SSL сертификаты для аутентификации
            # Дополнительная верификация не требуется
            is_valid = True
        else:  # O!Dengi
            webhook_signature = request.headers.get('X-O-Dengi-Signature', '')
            is_valid = get_payment_provider_service().verify_webhook(payload, webhook_signature)
        
        if not is_valid:
            logger.warning(f"Invalid webhook signature from {request.client.host} for provider {provider_name}")
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        # 3. Парсинг данных в зависимости от провайдера
        if provider_name == "OBANK":
            # Для OBANK парсим XML
            import xml.etree.ElementTree as ET
            root = ET.fromstring(payload.decode('utf-8'))
            
            # Извлекаем данные из XML
            invoice_id = root.find('.//invoice_id').text if root.find('.//invoice_id') is not None else None
            status = root.find('.//status').text if root.find('.//status') is not None else None
            amount = root.find('.//sum').text if root.find('.//sum') is not None else None
            
            # Преобразуем статус OBANK в числовой формат
            status_mapping = {"completed": 1, "failed": 2, "cancelled": 2}
            numeric_status = status_mapping.get(status, 0)
            paid_amount = float(amount) / 1000 if amount and status == "completed" else None
            
        else:  # O!Dengi
            webhook_data = PaymentWebhookData.parse_raw(payload)
            invoice_id = webhook_data.invoice_id
            numeric_status = webhook_data.status
            paid_amount = webhook_data.paid_amount / 100 if webhook_data.paid_amount else None
        
        # 4. Поиск платежа в базе
        topup_check = db.execute(text("""
            SELECT id, client_id, requested_amount, status, payment_provider FROM balance_topups 
            WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        topup = topup_check.fetchone()
        
        if not topup:
            logger.warning(f"Payment not found for webhook: {invoice_id}")
            return {"status": "payment_not_found"}
        
        # 5. Проверяем что провайдер соответствует записи в БД
        if topup[4] != provider_name:
            logger.warning(f"Provider mismatch for payment {invoice_id}: expected {topup[4]}, got {provider_name}")
            return {"status": "provider_mismatch"}
        
        # 6. Обработка пополнения баланса
        if numeric_status == 1 and topup[3] != "approved":  # Оплачено
            background_tasks.add_task(
                process_balance_topup,
                topup[0],  # topup_id
                topup[1],  # client_id
                paid_amount if paid_amount else topup[2],  # amount
                invoice_id,
                provider_name
            )
        
        return {"status": "received", "invoice_id": invoice_id, "provider": provider_name}
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Webhook processing failed")

@router.get("/balance/{client_id}", response_model=ClientBalanceInfo)
async def get_client_balance(
    client_id: str, 
    db: Session = Depends(get_db)
) -> ClientBalanceInfo:
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
            WHERE client_id = :client_id AND status = 'approved'
            ORDER BY paid_at DESC LIMIT 1
        """), {"client_id": client_id})
        
        last_topup_date = last_topup.fetchone()
        
        # Подсчитываем общую потраченную сумму (резерв минус возвраты плюс доплаты)
        total_spent = db.execute(text("""
            SELECT COALESCE(SUM(CASE 
                WHEN transaction_type = 'charge_reserve' THEN ABS(amount)
                WHEN transaction_type = 'charge_refund' THEN -ABS(amount) 
                WHEN transaction_type = 'charge_payment' THEN ABS(amount)
                ELSE 0 END), 0) 
            FROM payment_transactions_odengi 
            WHERE client_id = :client_id AND transaction_type IN ('charge_reserve', 'charge_refund', 'charge_payment')
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
# H2H И ТОКЕН-ПЛАТЕЖИ OBANK
# ============================================================================

@router.post("/balance/h2h-payment", response_model=H2HPaymentResponse)
async def create_h2h_payment(
    request: H2HPaymentRequest,
    db: Session = Depends(get_db)
) -> H2HPaymentResponse:
    """💳 Host2Host платеж картой (прямой ввод данных карты)"""
    try:
        # Проверяем что используется OBANK
        if settings.PAYMENT_PROVIDER != "OBANK":
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="h2h_not_supported",
                message="H2H платежи поддерживаются только через OBANK"
            )

        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        client = client_check.fetchone()
        if not client:
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="client_not_found"
            )

        # 2. Проверяем существующие processing платежи
        existing_pending = db.execute(text("""
            SELECT invoice_id FROM balance_topups 
            WHERE client_id = :client_id AND status = 'processing' 
            AND invoice_expires_at > NOW()
        """), {"client_id": request.client_id}).fetchone()
        
        if existing_pending:
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="pending_payment_exists"
            )

        # 3. Генерируем уникальный transaction ID
        transaction_id = f"h2h_{request.client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 4. Описание платежа
        description = request.description or f"H2H пополнение баланса клиента {request.client_id} на {request.amount} сом"
        
        # 5. Создаем H2H платеж через OBANK
        notify_url = f"{settings.API_V1_STR}/payment/webhook"
        redirect_url = f"{settings.API_V1_STR}/payment/success"
        
        h2h_response = await obank_service.create_h2h_payment(
            amount=Decimal(str(request.amount)),
            transaction_id=transaction_id,
            account=request.card_pan[-4:],  # Последние 4 цифры карты как account
            email=request.email,
            notify_url=notify_url,
            redirect_url=redirect_url,
            card_pan=request.card_pan,
            card_name=request.card_name,
            card_cvv=request.card_cvv,
            card_year=request.card_year,
            card_month=request.card_month,
            phone_number=request.phone_number
        )
        
        if h2h_response.get("code") != "0":
            return H2HPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="h2h_payment_failed",
                message=h2h_response.get("message", "Ошибка H2H платежа")
            )

        # 6. Сохраняем в базу данных
        auth_key = h2h_response.get("data", {}).get("auth-key", transaction_id)
        created_at = datetime.now(timezone.utc)
        qr_expires_at, invoice_expires_at = payment_lifecycle_service.calculate_expiry_times(created_at)

        db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, status, odengi_status,
             qr_expires_at, invoice_expires_at, needs_status_check, payment_provider)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, 'processing', 0,
                    :qr_expires_at, :invoice_expires_at, true, 'OBANK')
        """), {
            "invoice_id": auth_key,
            "order_id": transaction_id,
            "merchant_id": "OBANK",
            "client_id": request.client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_expires_at": qr_expires_at,
            "invoice_expires_at": invoice_expires_at
        })
        
        db.commit()
        
        logger.info(f"💳 H2H платеж создан: {transaction_id}, auth_key: {auth_key}")
        
        # Запускаем мониторинг статуса платежа
        async def check_h2h_payment_status():
            for i in range(20):
                await asyncio.sleep(15)
                try:
                    result = await payment_lifecycle_service.perform_status_check(
                        db, "balance_topups", auth_key
                    )
                    if result.get("success"):
                        new_status = result.get("new_status")
                        if new_status in ['approved', 'canceled', 'refunded']:
                            return
                except Exception as e:
                    logger.error(f"Ошибка проверки H2H платежа {auth_key}: {e}")
                    
        asyncio.create_task(check_h2h_payment_status())
        logger.info(f"🔍 Запущен мониторинг H2H платежа {auth_key}")
        
        return H2HPaymentResponse(
            success=True,
            transaction_id=transaction_id,
            auth_key=auth_key,
            status="processing",
            message="H2H платеж создан успешно",
            client_id=request.client_id,
            current_balance=float(client[1])
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка создания H2H платежа: {e}")
        return H2HPaymentResponse(
            success=False,
            client_id=request.client_id,
            error="internal_error"
        )

@router.post("/balance/token-payment", response_model=TokenPaymentResponse)
async def create_token_payment(
    request: TokenPaymentRequest,
    db: Session = Depends(get_db)
) -> TokenPaymentResponse:
    """🔐 Платеж по токену сохраненной карты"""
    try:
        # Проверяем что используется OBANK
        if settings.PAYMENT_PROVIDER != "OBANK":
            return TokenPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="token_payment_not_supported",
                message="Токен-платежи поддерживаются только через OBANK"
            )

        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        client = client_check.fetchone()
        if not client:
            return TokenPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="client_not_found"
            )

        # 2. Генерируем уникальный transaction ID
        transaction_id = f"token_{request.client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 3. Описание платежа
        description = request.description or f"Токен-пополнение баланса клиента {request.client_id} на {request.amount} сом"
        
        # 4. Создаем токен-платеж через OBANK
        notify_url = f"{settings.API_V1_STR}/payment/webhook"
        redirect_url = f"{settings.API_V1_STR}/payment/success"
        
        token_response = await obank_service.create_token_payment(
            amount=Decimal(str(request.amount)),
            transaction_id=transaction_id,
            email=request.email,
            notify_url=notify_url,
            redirect_url=redirect_url,
            card_token=request.card_token
        )
        
        if token_response.get("code") != "0":
            return TokenPaymentResponse(
                success=False,
                client_id=request.client_id,
                error="token_payment_failed",
                message=token_response.get("message", "Ошибка токен-платежа")
            )

        # 5. Сохраняем в базу данных
        auth_key = token_response.get("data", {}).get("auth-key", transaction_id)
        created_at = datetime.now(timezone.utc)
        qr_expires_at, invoice_expires_at = payment_lifecycle_service.calculate_expiry_times(created_at)

        db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, status, odengi_status,
             qr_expires_at, invoice_expires_at, needs_status_check, payment_provider)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, 'processing', 0,
                    :qr_expires_at, :invoice_expires_at, true, 'OBANK')
        """), {
            "invoice_id": auth_key,
            "order_id": transaction_id,
            "merchant_id": "OBANK",
            "client_id": request.client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_expires_at": qr_expires_at,
            "invoice_expires_at": invoice_expires_at
        })
        
        db.commit()
        
        logger.info(f"🔐 Токен-платеж создан: {transaction_id}, auth_key: {auth_key}")
        
        # Запускаем мониторинг статуса платежа
        async def check_token_payment_status():
            for i in range(20):
                await asyncio.sleep(15)
                try:
                    result = await payment_lifecycle_service.perform_status_check(
                        db, "balance_topups", auth_key
                    )
                    if result.get("success"):
                        new_status = result.get("new_status")
                        if new_status in ['approved', 'canceled', 'refunded']:
                            return
                except Exception as e:
                    logger.error(f"Ошибка проверки токен-платежа {auth_key}: {e}")
                    
        asyncio.create_task(check_token_payment_status())
        logger.info(f"🔍 Запущен мониторинг токен-платежа {auth_key}")
        
        return TokenPaymentResponse(
            success=True,
            transaction_id=transaction_id,
            auth_key=auth_key,
            status="processing",
            message="Токен-платеж создан успешно",
            client_id=request.client_id,
            current_balance=float(client[1])
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка создания токен-платежа: {e}")
        return TokenPaymentResponse(
            success=False,
            client_id=request.client_id,
            error="internal_error"
        )

@router.post("/payment/create-token", response_model=CreateTokenResponse)
async def create_card_token(
    request: CreateTokenRequest
) -> CreateTokenResponse:
    """🔑 Создание токена для сохранения карт"""
    try:
        # Проверяем что используется OBANK
        if settings.PAYMENT_PROVIDER != "OBANK":
            return CreateTokenResponse(
                success=False,
                error="token_creation_not_supported",
                message="Создание токенов поддерживается только через OBANK"
            )

        # Создаем токен через OBANK
        token_response = await obank_service.create_token(days=request.days)
        
        if token_response.get("code") != "0":
            return CreateTokenResponse(
                success=False,
                error="token_creation_failed",
                message=token_response.get("message", "Ошибка создания токена")
            )

        # Извлекаем URL для сохранения карты
        token_data = token_response.get("data", {})
        token_url = token_data.get("url", "")
        
        logger.info(f"🔑 Токен создан на {request.days} дней, URL: {token_url}")
        
        return CreateTokenResponse(
            success=True,
            token_url=token_url,
            token_expires_in_days=request.days,
            message=f"Токен создан на {request.days} дней"
        )
        
    except Exception as e:
        logger.error(f"Ошибка создания токена: {e}")
        return CreateTokenResponse(
            success=False,
            error="internal_error"
        )

# ============================================================================
# BACKGROUND TASKS ДЛЯ ОБРАБОТКИ ПЛАТЕЖЕЙ
# ============================================================================

async def process_balance_topup(topup_id: int, client_id: str, amount: float, invoice_id: str, provider: str = "ODENGI"):
    """Обработка успешного пополнения баланса"""
    try:
        with next(get_db()) as db:
            # Получаем текущий баланс
            current_balance = payment_service.get_client_balance(db, client_id)
            
            # Пополняем баланс
            new_balance = payment_service.update_client_balance(
                db, client_id, Decimal(str(amount)), "add",
                f"Пополнение баланса через {provider} (invoice: {invoice_id})"
            )
            
            # Создаем транзакцию
            payment_service.create_payment_transaction(
                db, client_id, "balance_topup", 
                Decimal(str(amount)), current_balance, new_balance,
                f"Пополнение баланса через {provider}",
                balance_topup_id=topup_id
            )
            
            # Обновляем статус пополнения
            db.execute(text("""
                UPDATE balance_topups 
                SET status = 'approved', paid_at = NOW(), paid_amount = :amount
                WHERE id = :topup_id
            """), {"amount": amount, "topup_id": topup_id})
            
            db.commit()
            
            logger.info(f"✅ Баланс пополнен: клиент {client_id}, сумма {amount}, новый баланс {new_balance}")
            
    except Exception as e:
        logger.error(f"Ошибка обработки пополнения баланса: {e}") 

@router.post("/balance/topup-qr", response_model=BalanceTopupResponse)
async def create_qr_balance_topup(
    request: BalanceTopupRequest, 
    db: Session = Depends(get_db)
) -> BalanceTopupResponse:
    """
    🔥 Пополнение баланса через QR код (O!Dengi)
    
    Принудительно использует O!Dengi для генерации QR кода
    """
    logger.info(f"🔥 QR Topup request: client_id={request.client_id}, amount={request.amount}")
    
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

        # 2. Отменяем существующие активные QR коды (улучшенный UX)
        existing_pending = db.execute(text("""
            SELECT invoice_id FROM balance_topups 
            WHERE client_id = :client_id AND status = 'processing' 
            AND invoice_expires_at > NOW()
        """), {"client_id": request.client_id}).fetchall()
        
        if existing_pending:
            # Отменяем все активные QR коды клиента
            cancelled_invoices = [row.invoice_id for row in existing_pending]
            db.execute(text("""
                UPDATE balance_topups 
                SET status = 'canceled'
                WHERE client_id = :client_id AND status = 'processing'
                AND invoice_expires_at > NOW()
            """), {"client_id": request.client_id})
            
            logger.info(f"🔄 Отменены активные QR коды для клиента {request.client_id}: {cancelled_invoices}")
            db.commit()

        # 3. Генерация безопасного order_id
        order_id = f"qr_topup_{request.client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 4. Описание платежа
        description = request.description or f"Пополнение баланса через QR код: {request.amount} сом"
        
        # 5. Принудительно используем O!Dengi для QR платежей
        qr_payment_provider = get_qr_payment_service()
        
        # 6. Создание платежа через O!Dengi
        notify_url = f"{settings.API_V1_STR}/payment/webhook"
        redirect_url = f"{settings.API_V1_STR}/payment/success"
        
        payment_response = await qr_payment_provider.create_payment(
            amount=Decimal(str(request.amount)),
            order_id=order_id,
            email=request.client_id + "@evpower.local",
            notify_url=notify_url,
            redirect_url=redirect_url,
            description=description,
            client_id=request.client_id
        )
        
        if not payment_response.get("success"):
            return BalanceTopupResponse(
                success=False,
                error="payment_provider_error",
                client_id=request.client_id
            )

        # 7. Получаем QR код из ODENGI ответа (по официальной документации)
        raw_response = payment_response.get("raw_response", {})
        qr_data = raw_response.get("data", {})
        
        # По документации ODENGI ответ должен содержать invoice_id и qr поля
        qr_code_data = qr_data.get("qr")  # URL изображения QR кода
        qr_code_url = qr_data.get("qr") or f"https://api.dengi.o.kg/qr.php?type=emvQr&data={qr_code_data}" if qr_code_data else None
        app_link_url = qr_data.get("link_app") or qr_data.get("app_link")
        
        logger.info(f"📱 ODENGI ответ: qr_data={qr_code_data[:50] if qr_code_data else None}...")
        logger.info(f"📱 ODENGI qr_url={qr_code_url}")
        logger.info(f"📱 ODENGI app_link={app_link_url}")
        
        # Если нет прямых данных QR, пытаемся извлечь из URL
        if not qr_code_data and qr_code_url:
            try:
                from urllib.parse import urlparse, parse_qs, unquote
                parsed_url = urlparse(qr_code_url)
                query_params = parse_qs(parsed_url.query)
                
                if 'data' in query_params and query_params['data']:
                    qr_code_data = unquote(query_params['data'][0])
                    logger.info(f"📱 Извлечены данные QR из URL: {qr_code_data[:50]}...")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось извлечь данные QR-кода из URL: {e}")
                qr_code_data = None
        
        # 8. Рассчитываем время жизни платежа
        created_at = datetime.now(timezone.utc)
        qr_expires_at, invoice_expires_at = payment_lifecycle_service.calculate_expiry_times(created_at)

        # 9. Сохранение в базу данных
        topup_insert = db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, qr_code_url, app_link, status, odengi_status,
             qr_expires_at, invoice_expires_at, needs_status_check, payment_provider)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, :qr_code_url, :app_link, 'processing', 0,
                    :qr_expires_at, :invoice_expires_at, true, :payment_provider)
            RETURNING id
        """), {
            "invoice_id": payment_response.get("invoice_id", payment_response.get("auth_key")),
            "order_id": order_id,
            "merchant_id": "ODENGI",
            "client_id": request.client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_code_url": qr_code_url,
            "app_link": app_link_url,
            "qr_expires_at": qr_expires_at,
            "invoice_expires_at": invoice_expires_at,
            "payment_provider": "ODENGI"
        })
        
        db.commit()
        
        invoice_id = payment_response.get("invoice_id", payment_response.get("auth_key"))
        logger.info(f"🔥 QR пополнение создано: {order_id}, invoice_id: {invoice_id}, QR истекает: {qr_expires_at}")
        
        # 10. Запускаем мониторинг статуса платежа
        async def check_payment_status_task():
            for i in range(20):
                await asyncio.sleep(15)
                try:
                    result = await payment_lifecycle_service.perform_status_check(
                        db, "balance_topups", invoice_id
                    )
                    if result.get("success"):
                        new_status = result.get("new_status")
                        if new_status in ['approved', 'canceled', 'refunded']:
                            return
                except Exception as e:
                    logger.error(f"Ошибка проверки QR платежа {invoice_id}: {e}")
                    
        asyncio.create_task(check_payment_status_task())
        logger.info(f"🔍 Запущен мониторинг QR платежа {invoice_id}")
        
        return BalanceTopupResponse(
            success=True,
            invoice_id=invoice_id,
            order_id=order_id,
            qr_code=qr_code_data,
            qr_code_url=qr_code_url,
            app_link=app_link_url,
            amount=request.amount,
            client_id=request.client_id,
            current_balance=float(client[1]),
            qr_expires_at=qr_expires_at,
            invoice_expires_at=invoice_expires_at,
            qr_lifetime_seconds=300,
            invoice_lifetime_seconds=600
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ QR Topup exception: {e}")
        return BalanceTopupResponse(
            success=False,
            error="internal_error",
            client_id=request.client_id
        )

@router.post("/balance/topup-card", response_model=H2HPaymentResponse)
async def create_card_balance_topup(
    request: H2HPaymentRequest,
    db: Session = Depends(get_db)
) -> H2HPaymentResponse:
    """
    💳 Пополнение баланса банковской картой (OBANK)
    
    Принудительно использует OBANK для H2H платежей
    """
    logger.info(f"Card Topup request received for client: {request.client_id}")
    
    try:
        # 1. Проверяем существование клиента
        client_check = db.execute(text("SELECT id, balance FROM clients WHERE id = :client_id"), 
                                {"client_id": request.client_id})
        client = client_check.fetchone()
        if not client:
            return H2HPaymentResponse(
                success=False,
                error="client_not_found",
                client_id=request.client_id
            )

        # 2. Принудительно используем OBANK для карт
        card_payment_provider = get_card_payment_service()
        
        # 3. Генерация безопасного order_id
        order_id = f"card_topup_{request.client_id}_{int(datetime.now(timezone.utc).timestamp())}"
        
        # 4. Описание платежа
        description = request.description or f"Пополнение баланса картой: {request.amount} сом"
        
        # 5. Создание H2H платежа через OBANK
        h2h_response = await card_payment_provider.create_h2h_payment(
            amount=Decimal(str(request.amount)),
            order_id=order_id,
            card_data={
                "pan": request.card_pan,
                "name": request.card_name,
                "cvv": request.card_cvv,
                "year": request.card_year,
                "month": request.card_month
            },
            email=request.email,
            phone_number=request.phone_number,
            description=description
        )
        
        if not h2h_response.get("success"):
            logger.error(f"❌ Card payment failed: {h2h_response.get('error')}")
            return H2HPaymentResponse(
                success=False,
                error=h2h_response.get("error", "payment_provider_error"),
                client_id=request.client_id
            )
        
        # 6. Сохраняем платеж в balance_topups с данными OBANK
        auth_key = h2h_response.get("auth_key")
        transaction_id = h2h_response.get("transaction_id")
        
        created_at = datetime.now(timezone.utc)
        qr_expires_at, invoice_expires_at = payment_lifecycle_service.calculate_expiry_times(created_at)
        
        topup_insert = db.execute(text("""
            INSERT INTO balance_topups 
            (invoice_id, order_id, merchant_id, client_id, requested_amount, 
             currency, description, status, odengi_status,
             qr_expires_at, invoice_expires_at, needs_status_check, payment_provider)
            VALUES (:invoice_id, :order_id, :merchant_id, :client_id, :requested_amount,
                    :currency, :description, 'processing', 0,
                    :qr_expires_at, :invoice_expires_at, true, :payment_provider)
            RETURNING id
        """), {
            "invoice_id": auth_key,  # Для OBANK используем auth_key как invoice_id
            "order_id": order_id,
            "merchant_id": "OBANK",
            "client_id": request.client_id,
            "requested_amount": request.amount,
            "currency": settings.DEFAULT_CURRENCY,
            "description": description,
            "qr_expires_at": qr_expires_at,
            "invoice_expires_at": invoice_expires_at,
            "payment_provider": "OBANK"
        })
        
        db.commit()
        
        logger.info(f"💳 Card пополнение создано: {order_id}, auth_key: {auth_key}, transaction_id: {transaction_id}")
        
        # 7. Запускаем мониторинг статуса H2H платежа
        async def check_h2h_payment_status():
            for i in range(20):
                await asyncio.sleep(15)
                try:
                    result = await payment_lifecycle_service.perform_status_check(
                        db, "balance_topups", auth_key
                    )
                    if result.get("success"):
                        new_status = result.get("new_status")
                        if new_status in ['approved', 'canceled', 'refunded']:
                            return
                except Exception as e:
                    logger.error(f"Ошибка проверки Card платежа {auth_key}: {e}")
                    
        asyncio.create_task(check_h2h_payment_status())
        logger.info(f"🔍 Запущен мониторинг Card платежа {auth_key}")
        
        return H2HPaymentResponse(
            success=True,
            transaction_id=transaction_id,
            auth_key=auth_key,
            status=h2h_response.get("status", "processing"),
            message=h2h_response.get("message", "Платеж создан"),
            client_id=request.client_id,
            current_balance=float(client[1])
        )
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Card Topup exception: {e}")
        return H2HPaymentResponse(
            success=False,
            error="internal_error",
            client_id=request.client_id
        )



@router.post("/payment/h2h-payment", response_model=H2HPaymentResponse)
async def create_h2h_payment_endpoint(
    request: H2HPaymentRequest,
    db: Session = Depends(get_db)
) -> H2HPaymentResponse:
    """
    Direct Host-to-Host card payment via OBANK API (XML format with SSL cert)
    
    Требует:
    - Клиентский SSL сертификат PKCS12
    - XML формат запроса
    - Mutual TLS authentication
    """
    try:
        logger.info(f"H2H payment request for client: {request.client_id}, amount: {request.amount}")
        
        card_data = {
            "number": request.card_pan,
            "holder_name": request.card_name,
            "cvv": request.card_cvv,
            "exp_month": request.card_month,
            "exp_year": request.card_year
        }
        
        # Direct H2H payment
        result = await obank_service.create_h2h_payment(
            amount_kgs=request.amount,
            client_id=request.client_id,
            card_data=card_data
        )
        
        return {
            "success": result.get("success", False),
            "payment_id": result.get("payment_id"),
            "status": result.get("status"),
            "detail": result.get("result")
        }
        
    except Exception as e:
        logger.error(f"H2H payment error: {str(e)}")
        raise HTTPException(status_code=500, detail="Ошибка создания платежа")

@router.post("/payment/create-token")
async def create_payment_token(
    days: int = 14,
    db: Session = Depends(get_db)
):
    """
    Create card storage token via OBANK API (XML format)
    
    Максимум 14 дней хранения токена
    """
    try:
        logger.info(f"Creating payment token for {days} days")
        
        result = await obank_service.create_token(days=days)
        
        return {
            "success": result.get("success", False),
            "detail": result.get("result")
        }
        
    except Exception as e:
        logger.error(f"Token creation error: {str(e)}")
        raise HTTPException(status_code=500, detail="Ошибка создания токена")

@router.post("/payment/token-payment")
async def token_payment(
    client_id: str,
    amount: float,
    card_token: str,
    db: Session = Depends(get_db)
):
    """
    Payment using saved card token via OBANK API (XML format)
    """
    try:
        logger.info(f"Token payment for client: {client_id}, amount: {amount}")
        
        result = await obank_service.create_token_payment(
            amount_kgs=amount,
            client_id=client_id,
            card_token=card_token
        )
        
        return {
            "success": result.get("success", False),
            "payment_id": result.get("payment_id"),
            "status": result.get("status"),
            "detail": result.get("result")
        }
        
    except Exception as e:
        logger.error(f"Token payment error: {str(e)}")
        raise HTTPException(status_code=500, detail="Ошибка токен-платежа")

@router.get("/payment/h2h-status/{transaction_id}")
async def check_h2h_payment_status(
    transaction_id: str,
    db: Session = Depends(get_db)
):
    """
    Check H2H payment status via OBANK API (XML format)
    """
    try:
        logger.info(f"Checking H2H payment status: {transaction_id}")
        
        result = await obank_service.check_h2h_status(transaction_id)
        
        return {
            "success": result.get("success", False),
            "status": result.get("status"),
            "final": result.get("final", False),
            "detail": result.get("result")
        }
        
    except Exception as e:
        logger.error(f"Status check error: {str(e)}")
        raise HTTPException(status_code=500, detail="Ошибка проверки статуса")

@router.post("/payment/cancel/{invoice_id}")
async def cancel_payment_manually(
    invoice_id: str,
    db: Session = Depends(get_db)
):
    """❌ Ручная отмена платежа (для тестирования)"""
    try:
        # 1. Ищем платеж
        payment_check = db.execute(text("""
            SELECT id, client_id, status, requested_amount, payment_provider
            FROM balance_topups WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        payment = payment_check.fetchone()
        if not payment:
            return {
                "success": False,
                "error": "payment_not_found",
                "message": "Платеж не найден"
            }
        
        payment_id, client_id, current_status, amount, provider = payment
        
        # 2. Проверяем можно ли отменить
        if current_status != "processing":
            return {
                "success": False,
                "error": "cannot_cancel",
                "message": f"Платеж нельзя отменить, текущий статус: {current_status}",
                "current_status": current_status
            }
        
        # 3. Отменяем платеж
        db.execute(text("""
            UPDATE balance_topups 
            SET status = 'canceled', 
                completed_at = NOW(),
                needs_status_check = false
            WHERE invoice_id = :invoice_id
        """), {"invoice_id": invoice_id})
        
        db.commit()
        
        logger.info(f"❌ Платеж {invoice_id} отменен вручную (клиент: {client_id}, сумма: {amount})")
        
        return {
            "success": True,
            "message": "Платеж успешно отменен",
            "invoice_id": invoice_id,
            "client_id": client_id,
            "amount": float(amount),
            "previous_status": current_status,
            "new_status": "canceled",
            "provider": provider
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка отмены платежа {invoice_id}: {e}")
        return {
            "success": False,
            "error": "internal_error",
            "message": f"Ошибка отмены: {str(e)}"
        }