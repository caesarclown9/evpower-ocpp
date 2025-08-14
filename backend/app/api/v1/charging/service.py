"""
Сервисный слой для операций зарядки
"""
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging

from app.crud.ocpp_service import payment_service

logger = logging.getLogger(__name__)

class ChargingService:
    """Сервис для управления сессиями зарядки"""
    
    def __init__(self, db: Session):
        self.db = db
    
    async def start_charging_session(
        self,
        client_id: str,
        station_id: str,
        connector_id: int,
        energy_kwh: Optional[float],
        amount_som: Optional[float],
        redis_manager: Any
    ) -> Dict[str, Any]:
        """Начать сессию зарядки с резервированием средств"""
        
        # 1. Проверка клиента и баланса
        client = self._validate_client(client_id)
        if not client['success']:
            return client
        
        # 2. Проверка станции и тарифов
        station_info = self._validate_station(station_id)
        if not station_info['success']:
            return station_info
        
        # 3. Расчет стоимости и резервирования
        reservation = self._calculate_reservation(
            client['balance'],
            station_info['rate_per_kwh'],
            energy_kwh,
            amount_som
        )
        if not reservation['success']:
            return reservation
        
        # 4. Проверка коннектора
        connector = self._validate_connector(station_id, connector_id)
        if not connector['success']:
            return connector
        
        # 5. Проверка активных сессий
        if self._has_active_session(client_id):
            return {
                "success": False,
                "error": "session_already_active",
                "message": "У вас уже есть активная сессия зарядки"
            }
        
        # 6. Резервирование средств
        new_balance = self._reserve_funds(
            client_id,
            reservation['amount'],
            station_id
        )
        
        # 7. Создание OCPP авторизации
        id_tag = self._setup_ocpp_authorization(client_id)
        
        # 8. Создание сессии
        session_id = self._create_charging_session(
            client_id,
            station_id,
            reservation,
            energy_kwh,
            amount_som
        )
        
        # 9. Обновление статуса коннектора
        self._update_connector_status(station_id, connector_id, 'occupied')
        
        # 10. Коммит транзакции
        self.db.commit()
        
        # 11. Отправка команды на станцию
        station_online = await self._send_start_command(
            redis_manager,
            station_id,
            connector_id,
            id_tag,
            session_id,
            reservation['limit_type'],
            reservation['limit_value']
        )
        
        return {
            "success": True,
            "session_id": session_id,
            "station_id": station_id,
            "client_id": client_id,
            "connector_id": connector_id,
            "energy_kwh": energy_kwh,
            "rate_per_kwh": station_info['rate_per_kwh'],
            "estimated_cost": reservation['amount'],
            "reserved_amount": reservation['amount'],
            "new_balance": float(new_balance),
            "message": "Зарядка запущена, средства зарезервированы" if station_online else "Сессия создана, средства зарезервированы. Зарядка начнется при подключении станции.",
            "station_online": station_online
        }
    
    def _validate_client(self, client_id: str) -> Dict[str, Any]:
        """Проверка существования клиента и его баланса"""
        result = self.db.execute(
            text("SELECT id, balance FROM clients WHERE id = :client_id"),
            {"client_id": client_id}
        ).fetchone()
        
        if not result:
            return {
                "success": False,
                "error": "client_not_found",
                "message": "Клиент не найден"
            }
        
        return {
            "success": True,
            "id": result[0],
            "balance": Decimal(str(result[1]))
        }
    
    def _validate_station(self, station_id: str) -> Dict[str, Any]:
        """Проверка станции и получение тарифа"""
        # Проверяем и административный статус (active) и доступность (is_available)
        result = self.db.execute(text("""
            SELECT s.id, s.status, s.price_per_kwh, tp.id as tariff_plan_id, 
                   s.is_available, s.last_heartbeat_at
            FROM stations s
            LEFT JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.id = :station_id AND s.status = 'active'
        """), {"station_id": station_id}).fetchone()
        
        if not result:
            return {
                "success": False,
                "error": "station_not_found",
                "message": "Станция не найдена или отключена администратором"
            }
        
        # Проверяем доступность по heartbeat
        if not result[4]:  # is_available = false
            last_heartbeat = result[5]
            if last_heartbeat:
                from datetime import datetime, timezone
                minutes_ago = (datetime.now(timezone.utc) - last_heartbeat).total_seconds() / 60
                return {
                    "success": False,
                    "error": "station_offline",
                    "message": f"Станция недоступна (офлайн {int(minutes_ago)} минут)"
                }
            else:
                return {
                    "success": False,
                    "error": "station_never_connected",
                    "message": "Станция никогда не подключалась к системе"
                }
        
        # Определение тарифа: приоритет станции над тарифным планом
        rate_per_kwh = 9.0  # fallback
        
        if result[2]:  # price_per_kwh станции
            rate_per_kwh = float(result[2])
        elif result[3]:  # tariff_plan_id
            tariff = self.db.execute(text("""
                SELECT price FROM tariff_rules 
                WHERE tariff_plan_id = :tariff_plan_id 
                AND tariff_type = 'per_kwh' 
                AND is_active = true
                ORDER BY priority DESC LIMIT 1
            """), {"tariff_plan_id": result[3]}).fetchone()
            
            if tariff:
                rate_per_kwh = float(tariff[0])
        
        return {
            "success": True,
            "id": result[0],
            "status": result[1],
            "rate_per_kwh": rate_per_kwh
        }
    
    def _calculate_reservation(
        self,
        balance: Decimal,
        rate_per_kwh: float,
        energy_kwh: Optional[float],
        amount_som: Optional[float]
    ) -> Dict[str, Any]:
        """Расчет суммы резервирования и лимитов"""
        
        if energy_kwh and amount_som:
            # Режим 1: Лимит по энергии + максимальная сумма
            estimated_cost = energy_kwh * rate_per_kwh
            reservation_amount = min(estimated_cost, amount_som)
            limit_type = 'energy'
            limit_value = energy_kwh
            
        elif amount_som:
            # Режим 2: Лимит только по сумме
            if amount_som > float(balance):
                return {
                    "success": False,
                    "error": "amount_exceeds_balance",
                    "message": f"Указанная сумма ({amount_som} сом) превышает баланс ({balance} сом)",
                    "current_balance": float(balance),
                    "requested_amount": amount_som
                }
            reservation_amount = min(float(balance), amount_som)
            limit_type = 'amount'
            limit_value = amount_som
            
        elif energy_kwh:
            # Режим 3: Лимит только по энергии
            reservation_amount = energy_kwh * rate_per_kwh
            limit_type = 'energy'
            limit_value = energy_kwh
            
        else:
            # Режим 4: Безлимитная зарядка
            max_reservation = 200.0
            reservation_amount = min(float(balance), max_reservation)
            
            if balance <= 0:
                return {
                    "success": False,
                    "error": "zero_balance",
                    "message": "Недостаточно средств для безлимитной зарядки",
                    "current_balance": float(balance)
                }
            
            min_reservation = 10.0
            if reservation_amount < min_reservation:
                return {
                    "success": False,
                    "error": "insufficient_balance",
                    "message": f"Минимальный резерв для безлимитной зарядки: {min_reservation} сом",
                    "current_balance": float(balance),
                    "required_amount": min_reservation
                }
            
            limit_type = 'none'
            limit_value = 0
        
        # Финальная проверка баланса
        if balance < Decimal(str(reservation_amount)):
            return {
                "success": False,
                "error": "insufficient_balance",
                "message": f"Недостаточно средств. Баланс: {balance} сом, требуется: {reservation_amount} сом",
                "current_balance": float(balance),
                "required_amount": reservation_amount
            }
        
        return {
            "success": True,
            "amount": reservation_amount,
            "limit_type": limit_type,
            "limit_value": limit_value
        }
    
    def _validate_connector(self, station_id: str, connector_id: int) -> Dict[str, Any]:
        """Проверка доступности коннектора"""
        result = self.db.execute(text("""
            SELECT connector_number, status FROM connectors 
            WHERE station_id = :station_id AND connector_number = :connector_id
        """), {"station_id": station_id, "connector_id": connector_id}).fetchone()
        
        if not result:
            return {
                "success": False,
                "error": "connector_not_found",
                "message": "Коннектор не найден"
            }
        
        if result[1] != "available":
            return {
                "success": False,
                "error": "connector_occupied",
                "message": "Коннектор занят или неисправен"
            }
        
        return {"success": True}
    
    def _has_active_session(self, client_id: str) -> bool:
        """Проверка наличия активной сессии"""
        result = self.db.execute(text("""
            SELECT id FROM charging_sessions 
            WHERE user_id = :client_id AND status = 'started'
        """), {"client_id": client_id}).fetchone()
        
        return result is not None
    
    def _reserve_funds(self, client_id: str, amount: float, station_id: str) -> Decimal:
        """Резервирование средств на балансе"""
        return payment_service.update_client_balance(
            self.db, client_id, Decimal(str(amount)), "subtract",
            f"Резервирование средств для зарядки на станции {station_id}"
        )
    
    def _setup_ocpp_authorization(self, client_id: str) -> str:
        """Создание OCPP авторизации"""
        # Получаем телефон клиента для id_tag
        phone_result = self.db.execute(
            text("SELECT phone FROM clients WHERE id = :client_id"),
            {"client_id": client_id}
        ).fetchone()
        
        id_tag = phone_result[0] if phone_result else f"CLIENT_{client_id}"
        
        # Проверяем существование авторизации
        auth_exists = self.db.execute(
            text("SELECT id_tag FROM ocpp_authorization WHERE id_tag = :id_tag"),
            {"id_tag": id_tag}
        ).fetchone()
        
        if not auth_exists:
            self.db.execute(text("""
                INSERT INTO ocpp_authorization (id_tag, status, parent_id_tag, client_id) 
                VALUES (:id_tag, 'Accepted', NULL, :client_id)
            """), {"id_tag": id_tag, "client_id": client_id})
        
        return id_tag
    
    def _create_charging_session(
        self,
        client_id: str,
        station_id: str,
        reservation: Dict[str, Any],
        energy_kwh: Optional[float],
        amount_som: Optional[float]
    ) -> str:
        """Создание сессии зарядки в БД"""
        result = self.db.execute(text("""
            INSERT INTO charging_sessions 
            (user_id, station_id, start_time, status, limit_type, limit_value, amount)
            VALUES (:user_id, :station_id, :start_time, 'started', :limit_type, :limit_value, :amount)
            RETURNING id
        """), {
            "user_id": client_id,
            "station_id": station_id,
            "start_time": datetime.now(timezone.utc),
            "limit_type": reservation['limit_type'],
            "limit_value": reservation['limit_value'],
            "amount": reservation['amount']
        }).fetchone()[0]
        
        # Логируем транзакцию резервирования
        current_balance = self._validate_client(client_id)['balance']
        new_balance = current_balance - Decimal(str(reservation['amount']))
        
        payment_service.create_payment_transaction(
            self.db, client_id, "charge_reserve",
            -Decimal(str(reservation['amount'])), current_balance, new_balance,
            f"Резервирование средств для сессии {result}",
            charging_session_id=result
        )
        
        return result
    
    def _update_connector_status(self, station_id: str, connector_id: int, status: str):
        """Обновление статуса коннектора"""
        self.db.execute(text("""
            UPDATE connectors 
            SET status = :status 
            WHERE station_id = :station_id AND connector_number = :connector_id
        """), {
            "station_id": station_id,
            "connector_id": connector_id,
            "status": status
        })
    
    async def _send_start_command(
        self,
        redis_manager: Any,
        station_id: str,
        connector_id: int,
        id_tag: str,
        session_id: str,
        limit_type: str,
        limit_value: float
    ) -> bool:
        """Отправка команды запуска на станцию через Redis"""
        connected_stations = await redis_manager.get_stations()
        is_online = station_id in connected_stations
        
        if is_online:
            command_data = {
                "action": "RemoteStartTransaction",
                "connector_id": connector_id,
                "id_tag": id_tag,
                "session_id": session_id,
                "limit_type": limit_type,
                "limit_value": limit_value
            }
            
            await redis_manager.publish_command(station_id, command_data)
            logger.info(f"✅ Команда запуска отправлена на станцию {station_id}")
        
        return is_online
    
    async def stop_charging_session(
        self,
        session_id: str,
        redis_manager: Any
    ) -> Dict[str, Any]:
        """Остановить сессию зарядки с расчетом и возвратом средств"""
        
        # 1. Получение информации о сессии
        session_info = self._get_session_info(session_id)
        if not session_info:
            return {
                "success": False,
                "error": "session_not_found",
                "message": "Активная сессия зарядки не найдена"
            }
        
        # 2. Расчет фактического потребления
        actual_energy = self._get_actual_energy_consumed(session_id, session_info.get('energy'))
        
        # 3. Расчет стоимости
        rate_per_kwh = self._get_session_rate(session_info)
        actual_cost = Decimal(str(actual_energy * rate_per_kwh))
        reserved_amount = Decimal(str(session_info['reserved_amount']))
        
        # 4. Обработка превышения резерва или возврата
        refund_amount, additional_charge = self._calculate_refund_or_charge(
            session_info['client_id'],
            actual_cost,
            reserved_amount,
            session_id
        )
        
        # 5. Обновление баланса
        new_balance = self._process_session_payment(
            session_info['client_id'],
            refund_amount,
            additional_charge,
            session_id,
            actual_energy
        )
        
        # 6. Обновление сессии в БД
        self._finalize_session(session_id, actual_energy, float(actual_cost))
        
        # 7. Освобождение коннектора
        self._update_connector_status(session_info['station_id'], 1, 'available')
        
        # 8. Отправка команды остановки
        station_online = await self._send_stop_command(
            redis_manager,
            session_info['station_id'],
            session_id
        )
        
        # 9. Коммит транзакции
        self.db.commit()
        
        logger.info(f"✅ Зарядка остановлена: сессия {session_id}, потреблено {actual_energy} кВт⋅ч")
        
        return {
            "success": True,
            "session_id": session_id,
            "station_id": session_info['station_id'],
            "client_id": session_info['client_id'],
            "start_time": session_info['start_time'].isoformat() if session_info['start_time'] else None,
            "stop_time": datetime.now(timezone.utc).isoformat(),
            "energy_consumed": actual_energy,
            "rate_per_kwh": rate_per_kwh,
            "reserved_amount": float(reserved_amount),
            "actual_cost": float(actual_cost),
            "refund_amount": float(refund_amount),
            "new_balance": float(new_balance),
            "message": f"Зарядка завершена. Потреблено {actual_energy} кВт⋅ч",
            "station_online": station_online
        }
    
    def _get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Получение информации о сессии"""
        result = self.db.execute(text("""
            SELECT cs.id, cs.user_id, cs.station_id, cs.start_time, cs.status,
                   cs.limit_value, cs.amount, cs.energy, s.price_per_kwh,
                   tp.id as tariff_plan_id
            FROM charging_sessions cs
            LEFT JOIN stations s ON cs.station_id = s.id
            LEFT JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE cs.id = :session_id AND cs.status = 'started'
        """), {"session_id": session_id}).fetchone()
        
        if not result:
            return None
        
        return {
            'id': result[0],
            'client_id': result[1],
            'station_id': result[2],
            'start_time': result[3],
            'status': result[4],
            'limit_value': result[5],
            'reserved_amount': result[6] or 0,
            'energy': result[7],
            'price_per_kwh': result[8],
            'tariff_plan_id': result[9]
        }
    
    def _get_actual_energy_consumed(self, session_id: str, session_energy: Optional[float]) -> float:
        """Получение фактически потребленной энергии"""
        if session_energy:
            return float(session_energy)
        
        # Пытаемся получить из OCPP транзакций
        result = self.db.execute(text("""
            SELECT COALESCE(ot.meter_stop - ot.meter_start, 0) as consumed_energy
            FROM ocpp_transactions ot
            WHERE ot.charging_session_id = :session_id
            ORDER BY ot.created_at DESC LIMIT 1
        """), {"session_id": session_id}).fetchone()
        
        return float(result[0]) if result and result[0] else 0.0
    
    def _get_session_rate(self, session_info: Dict[str, Any]) -> float:
        """Получение тарифа для сессии"""
        if session_info['price_per_kwh']:
            return float(session_info['price_per_kwh'])
        
        if session_info['tariff_plan_id']:
            result = self.db.execute(text("""
                SELECT price FROM tariff_rules
                WHERE tariff_plan_id = :tariff_plan_id
                AND tariff_type = 'per_kwh'
                AND is_active = true
                ORDER BY priority DESC LIMIT 1
            """), {"tariff_plan_id": session_info['tariff_plan_id']}).fetchone()
            
            if result:
                return float(result[0])
        
        return 9.0  # Default rate
    
    def _calculate_refund_or_charge(
        self,
        client_id: str,
        actual_cost: Decimal,
        reserved_amount: Decimal,
        session_id: str
    ) -> tuple[Decimal, Decimal]:
        """Расчет возврата или дополнительного списания"""
        additional_charge = Decimal('0')
        refund_amount = Decimal('0')
        
        if actual_cost > reserved_amount:
            # Требуется доплата
            additional_charge = actual_cost - reserved_amount
            current_balance = payment_service.get_client_balance(self.db, client_id)
            
            if current_balance < additional_charge:
                logger.warning(f"⚠️ Недостаток средств для доплаты в сессии {session_id}")
                additional_charge = current_balance
        else:
            # Возврат неиспользованных средств
            refund_amount = reserved_amount - actual_cost
        
        return refund_amount, additional_charge
    
    def _process_session_payment(
        self,
        client_id: str,
        refund_amount: Decimal,
        additional_charge: Decimal,
        session_id: str,
        energy_consumed: float
    ) -> Decimal:
        """Обработка платежей сессии"""
        current_balance = payment_service.get_client_balance(self.db, client_id)
        
        if additional_charge > 0:
            # Дополнительное списание
            new_balance = payment_service.update_client_balance(
                self.db, client_id, additional_charge, "subtract",
                f"Дополнительное списание за превышение резерва в сессии {session_id}"
            )
            
            payment_service.create_payment_transaction(
                self.db, client_id, "charge_payment",
                -additional_charge, current_balance, new_balance,
                f"Доплата за сессию {session_id}",
                charging_session_id=session_id
            )
        elif refund_amount > 0:
            # Возврат средств
            new_balance = payment_service.update_client_balance(
                self.db, client_id, refund_amount, "add",
                f"Возврат неиспользованных средств за сессию {session_id}"
            )
            
            payment_service.create_payment_transaction(
                self.db, client_id, "charge_refund",
                refund_amount, current_balance, new_balance,
                f"Возврат за сессию {session_id}: потреблено {energy_consumed} кВт⋅ч",
                charging_session_id=session_id
            )
        else:
            new_balance = current_balance
        
        return new_balance
    
    def _finalize_session(self, session_id: str, actual_energy: float, actual_cost: float):
        """Финализация сессии в БД"""
        self.db.execute(text("""
            UPDATE charging_sessions
            SET stop_time = NOW(), status = 'stopped',
                energy = :actual_energy, amount = :actual_cost
            WHERE id = :session_id
        """), {
            "actual_energy": actual_energy,
            "actual_cost": actual_cost,
            "session_id": session_id
        })
    
    async def _send_stop_command(
        self,
        redis_manager: Any,
        station_id: str,
        session_id: str
    ) -> bool:
        """Отправка команды остановки на станцию"""
        connected_stations = await redis_manager.get_stations()
        is_online = station_id in connected_stations
        
        if is_online:
            # Получаем OCPP transaction_id
            result = self.db.execute(text("""
                SELECT transaction_id FROM ocpp_transactions
                WHERE charging_session_id = :session_id
                AND status = 'Started'
                ORDER BY created_at DESC LIMIT 1
            """), {"session_id": session_id}).fetchone()
            
            if result:
                command_data = {
                    "action": "RemoteStopTransaction",
                    "transaction_id": result[0]
                }
                await redis_manager.publish_command(station_id, command_data)
                logger.info(f"✅ Команда остановки отправлена на станцию {station_id}")
        
        return is_online
    
    async def get_charging_status(self, session_id: str) -> Dict[str, Any]:
        """Получить полный статус сессии зарядки с OCPP данными"""
        
        logger.info(f"📊 Запрос статуса зарядки для сессии: {session_id}")
        
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
            
            logger.debug(f"Выполняем SQL запрос для сессии {session_id}")
            session_result = self.db.execute(session_query, {"session_id": session_id})
            session = session_result.fetchone()
            
            if not session:
                logger.warning(f"Сессия {session_id} не найдена в БД")
                return {
                    "success": False,
                    "error": "session_not_found",
                    "message": "Сессия зарядки не найдена"
                }
            
            logger.debug(f"Найдена сессия: status={session[7]}, station={session[2]}")
            
            # Парсинг данных сессии
            session_data = self._parse_session_data(session)
            
            # Расчет реальных данных из OCPP
            energy_data = self._calculate_energy_from_ocpp(session_data, session_id)
            
            # Расчет прогресса
            progress = self._calculate_progress(session_data, energy_data)
            
            # Получение расширенных meter данных
            meter_data = self._get_extended_meter_data(session_data.get('ocpp_transaction_id'))
            
            # Проверка статуса станции онлайн
            station_online = await self._check_station_online(session_data['station_id'])
            
            logger.info(f"✅ Статус получен: energy={energy_data.get('energy_consumed_kwh', 0)} кВт⋅ч, online={station_online}")
            
            return self._build_status_response(session_data, energy_data, progress, meter_data, station_online)
            
        except ValueError as e:
            logger.error(f"Ошибка валидации данных: {e}")
            return {
                "success": False,
                "error": "data_error",
                "message": str(e)
            }
        except Exception as e:
            logger.error(f"Критическая ошибка при получении статуса зарядки: {e}", exc_info=True)
            return {
                "success": False,
                "error": "internal_error",
                "message": "Внутренняя ошибка сервера"
            }
    
    def _get_active_session(self, session_id: str) -> Dict[str, Any]:
        """Поиск активной сессии зарядки"""
        session_query = text("""
            SELECT cs.id, cs.user_id, cs.station_id, cs.start_time, cs.status, 
                   cs.limit_value, cs.amount, cs.energy, s.price_per_kwh,
                   tp.id as tariff_plan_id
            FROM charging_sessions cs
            LEFT JOIN stations s ON cs.station_id = s.id
            LEFT JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE cs.id = :session_id AND cs.status = 'started'
        """)
        
        session_result = self.db.execute(session_query, {"session_id": session_id})
        session = session_result.fetchone()
        
        if not session:
            return {
                "success": False,
                "error": "session_not_found",
                "message": "Активная сессия зарядки не найдена"
            }
        
        return {
            "success": True,
            "data": {
                "session_id": session[0],
                "user_id": session[1],
                "station_id": session[2],
                "start_time": session[3],
                "status": session[4],
                "limit_value": session[5],
                "reserved_amount": session[6],
                "actual_energy": session[7],
                "price_per_kwh": session[8],
                "tariff_plan_id": session[9]
            }
        }
    
    def _get_actual_tariff(self, session_data: Dict[str, Any]) -> float:
        """Определение актуального тарифа"""
        rate_per_kwh = float(session_data['price_per_kwh'])
        
        if session_data['tariff_plan_id']:
            tariff_check = self.db.execute(text("""
                SELECT price FROM tariff_rules 
                WHERE tariff_plan_id = :tariff_plan_id 
                AND tariff_type = 'per_kwh' 
                AND is_active = true
                ORDER BY priority DESC LIMIT 1
            """), {"tariff_plan_id": session_data['tariff_plan_id']})
            
            tariff_rule = tariff_check.fetchone()
            if tariff_rule:
                rate_per_kwh = float(tariff_rule[0])
        
        return rate_per_kwh
    
    def _get_actual_energy_consumption(self, session_id: str, session_data: Dict[str, Any]) -> float:
        """Получение фактического потребления энергии"""
        actual_energy_consumed = float(session_data['actual_energy']) if session_data['actual_energy'] else 0
        
        # Если энергия не записана в сессии, получаем из OCPP транзакций
        if actual_energy_consumed == 0:
            ocpp_energy_query = text("""
                SELECT COALESCE(ot.meter_stop - ot.meter_start, 0) as consumed_energy
                FROM ocpp_transactions ot
                WHERE ot.charging_session_id = :session_id
                ORDER BY ot.created_at DESC LIMIT 1
            """)
            
            ocpp_result = self.db.execute(ocpp_energy_query, {"session_id": session_id})
            ocpp_energy = ocpp_result.fetchone()
            
            if ocpp_energy and ocpp_energy[0]:
                actual_energy_consumed = float(ocpp_energy[0])
        
        return actual_energy_consumed
    
    def _calculate_actual_cost(self, energy_consumed: float, rate_per_kwh: float, reserved_amount: float) -> Dict[str, Any]:
        """Расчет фактической стоимости и возврата"""
        actual_cost = energy_consumed * rate_per_kwh
        reserved_amount_decimal = Decimal(str(reserved_amount)) if reserved_amount else Decimal('0')
        actual_cost_decimal = Decimal(str(actual_cost))
        
        additional_charge = Decimal('0')
        if actual_cost_decimal > reserved_amount_decimal:
            additional_charge = actual_cost_decimal - reserved_amount_decimal
        
        refund_amount = Decimal('0')
        if additional_charge == 0:
            refund_amount = reserved_amount_decimal - actual_cost_decimal
            if refund_amount < 0:
                refund_amount = Decimal('0')
        
        return {
            "actual_cost": float(actual_cost_decimal),
            "additional_charge": additional_charge,
            "refund_amount": refund_amount
        }
    
    async def _process_balance_adjustment(self, user_id: str, cost_calculation: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """Обработка дополнительных списаний или возвратов"""
        current_balance = payment_service.get_client_balance(self.db, user_id)
        additional_charge = cost_calculation['additional_charge']
        refund_amount = cost_calculation['refund_amount']
        
        if additional_charge > 0:
            # Дополнительное списание
            if current_balance < additional_charge:
                logger.warning(f"⚠️ НЕДОСТАТОК СРЕДСТВ для доплаты в сессии {session_id}")
                additional_charge = current_balance
                cost_calculation['actual_cost'] = float(Decimal(str(cost_calculation['actual_cost'])) - 
                                                      (cost_calculation['additional_charge'] - additional_charge))
            
            if additional_charge > 0:
                new_balance = payment_service.update_client_balance(
                    self.db, user_id, additional_charge, "subtract",
                    f"Дополнительное списание за превышение резерва в сессии {session_id}"
                )
                
                payment_service.create_payment_transaction(
                    self.db, user_id, "charge_payment",
                    -additional_charge, current_balance, new_balance,
                    f"Доплата за сессию {session_id}: превышение резерва на {additional_charge} сом",
                    charging_session_id=session_id
                )
                
                logger.info(f"💳 ДОПОЛНИТЕЛЬНОЕ СПИСАНИЕ в сессии {session_id}: {additional_charge} сом")
        elif refund_amount > 0:
            # Возврат неиспользованных средств
            new_balance = payment_service.update_client_balance(
                self.db, user_id, refund_amount, "add",
                f"Возврат неиспользованных средств за сессию {session_id}"
            )
            
            payment_service.create_payment_transaction(
                self.db, user_id, "charge_refund",
                refund_amount, current_balance, new_balance,
                f"Возврат за сессию {session_id}: потреблено {cost_calculation.get('energy_consumed', 0)} кВт⋅ч",
                charging_session_id=session_id
            )
        else:
            new_balance = current_balance
        
        return {"new_balance": new_balance}
    
    def _finalize_session(self, session_id: str, energy_consumed: float, actual_cost: float):
        """Обновление сессии и освобождение коннектора"""
        # Обновляем сессию
        update_session = text("""
            UPDATE charging_sessions 
            SET stop_time = NOW(), status = 'stopped', 
                energy = :actual_energy, amount = :actual_cost
            WHERE id = :session_id
        """)
        
        self.db.execute(update_session, {
            "actual_energy": energy_consumed,
            "actual_cost": actual_cost,
            "session_id": session_id
        })
        
        # Освобождаем коннектор
        connector_update = text("""
            UPDATE connectors 
            SET status = 'available' 
            WHERE station_id = (
                SELECT station_id FROM charging_sessions 
                WHERE id = :session_id
            )
        """)
        self.db.execute(connector_update, {"session_id": session_id})
    
    async def _send_stop_command(self, redis_manager: Any, session_data: Dict[str, Any], session_id: str) -> bool:
        """Отправка команды остановки через Redis"""
        try:
            connected_stations = await redis_manager.get_stations()
            is_station_online = session_data['station_id'] in connected_stations
            
            if is_station_online:
                # Получаем OCPP transaction_id
                ocpp_transaction_query = text("""
                    SELECT transaction_id FROM ocpp_transactions 
                    WHERE charging_session_id = :session_id 
                    AND status = 'Started'
                    ORDER BY created_at DESC LIMIT 1
                """)
                
                ocpp_result = self.db.execute(ocpp_transaction_query, {"session_id": session_id})
                ocpp_transaction = ocpp_result.fetchone()
                
                if ocpp_transaction:
                    command_data = {
                        "action": "RemoteStopTransaction",
                        "transaction_id": ocpp_transaction[0]
                    }
                    
                    await redis_manager.publish_command(session_data['station_id'], command_data)
            
            return is_station_online
        except Exception as e:
            logger.warning(f"Ошибка отправки команды остановки: {e}")
            return False
    
    def _parse_session_data(self, session: tuple) -> Dict[str, Any]:
        """Парсинг данных сессии из результата запроса"""
        try:
            return {
                "session_id": session[0] if session[0] is not None else "",
                "user_id": session[1] if session[1] is not None else "",
                "station_id": session[2] if session[2] is not None else "",
                "start_time": session[3],
                "stop_time": session[4],
                "energy": float(session[5]) if session[5] is not None else 0.0,
                "amount": float(session[6]) if session[6] is not None else 0.0,
                "status": session[7] if session[7] is not None else "unknown",
                "transaction_id": session[8],
                "limit_type": session[9] if session[9] is not None else "none",
                "limit_value": float(session[10]) if session[10] is not None else 0.0,
                "ocpp_transaction_id": str(session[11]) if session[11] is not None else None,
                "meter_start": session[12],
                "meter_stop": session[13],
                "ocpp_status": session[14],
                "price_per_kwh": float(session[15]) if session[15] is not None else 9.0
            }
        except (IndexError, TypeError, ValueError) as e:
            logger.error(f"Ошибка парсинга данных сессии: {e}, данные: {session[:5] if session else 'None'}")
            raise ValueError(f"Некорректные данные сессии: {e}")
    
    def _calculate_energy_from_ocpp(self, session_data: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """Расчет реальных энергетических данных из OCPP"""
        actual_energy_consumed = float(session_data['energy'])
        actual_cost = actual_energy_consumed * float(session_data['price_per_kwh'])
        
        # Если есть OCPP данные - используем их для более точного расчета
        if session_data['meter_start'] is not None and session_data['meter_stop'] is not None:
            ocpp_energy_wh = float(session_data['meter_stop']) - float(session_data['meter_start'])
            actual_energy_consumed = max(ocpp_energy_wh / 1000.0, actual_energy_consumed)
            actual_cost = actual_energy_consumed * float(session_data['price_per_kwh'])
        elif session_data['meter_start'] is not None and session_data['status'] == 'started':
            # Активная зарядка - получаем последние показания
            latest_meter_query = text("""
                SELECT mv.energy_active_import_register
                FROM ocpp_meter_values mv
                JOIN ocpp_transactions ot ON mv.ocpp_transaction_id = ot.transaction_id
                WHERE ot.charging_session_id = :session_id
                AND mv.energy_active_import_register IS NOT NULL
                ORDER BY mv.timestamp DESC LIMIT 1
            """)
            latest_result = self.db.execute(latest_meter_query, {"session_id": session_id})
            latest_meter = latest_result.fetchone()
            
            if latest_meter and latest_meter[0]:
                current_meter = float(latest_meter[0])
                ocpp_energy_wh = current_meter - float(session_data['meter_start'])
                actual_energy_consumed = max(ocpp_energy_wh / 1000.0, actual_energy_consumed)
                actual_cost = actual_energy_consumed * float(session_data['price_per_kwh'])
        
        return {
            "actual_energy_consumed": actual_energy_consumed,
            "actual_cost": actual_cost
        }
    
    def _calculate_progress(self, session_data: Dict[str, Any], energy_data: Dict[str, Any]) -> Dict[str, Any]:
        """Расчет прогресса зарядки и длительности"""
        progress_percent = 0
        if session_data['limit_type'] == "energy" and session_data['limit_value'] > 0:
            progress_percent = min(100, (energy_data['actual_energy_consumed'] / float(session_data['limit_value'])) * 100)
        elif session_data['limit_type'] == "amount" and session_data['limit_value'] > 0:
            progress_percent = min(100, (energy_data['actual_cost'] / float(session_data['limit_value'])) * 100)
        
        # Длительность в минутах
        duration_minutes = 0
        if session_data['start_time']:
            end_time = session_data['stop_time'] or datetime.now(timezone.utc)
            duration_minutes = int((end_time - session_data['start_time']).total_seconds() / 60)
        
        return {
            "progress_percent": progress_percent,
            "duration_minutes": duration_minutes
        }
    
    def _get_extended_meter_data(self, ocpp_transaction_id: str) -> Dict[str, Any]:
        """Получение расширенных показаний датчиков"""
        if not ocpp_transaction_id:
            return {}
        
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
        
        latest_result = self.db.execute(latest_meter_query, {"transaction_id": ocpp_transaction_id})
        latest_meter = latest_result.fetchone()
        
        if not latest_meter:
            return {}
        
        meter_data = {
            'energy_register': latest_meter[0],
            'power': latest_meter[1], 
            'current': latest_meter[2],
            'voltage': latest_meter[3],
            'temperature': latest_meter[4],
            'soc': latest_meter[5],
            'timestamp': latest_meter[6],
            'sampled_values': latest_meter[7]
        }
        
        # Парсинг дополнительных данных из sampled_values JSON
        additional_data = self._parse_sampled_values(meter_data.get('sampled_values', []))
        meter_data.update(additional_data)
        
        return meter_data
    
    def _parse_sampled_values(self, sampled_values) -> Dict[str, Any]:
        """Парсинг дополнительных показаний из JSON"""
        ev_current = 0.0
        ev_voltage = 0.0
        station_body_temp = 0
        station_outlet_temp = 0
        station_inlet_temp = 0
        
        if sampled_values and isinstance(sampled_values, list):
            try:
                for sample in sampled_values:
                    measurand = sample.get('measurand', '')
                    value = self._safe_float(sample.get('value'), 0.0)
                    
                    if measurand == 'Current.Export':
                        ev_current = value
                    elif measurand == 'Voltage.Export':
                        ev_voltage = value
                    elif measurand == 'Temperature.Outlet':
                        station_outlet_temp = self._safe_int(value, 0)
                    elif measurand == 'Temperature.Inlet':
                        station_inlet_temp = self._safe_int(value, 0)
                    elif measurand == 'Temperature':
                        station_body_temp = self._safe_int(value, 0)
            except Exception as e:
                logger.warning(f"Ошибка парсинга sampled_values: {e}")
        
        return {
            "ev_current": ev_current,
            "ev_voltage": ev_voltage,
            "station_body_temp": station_body_temp,
            "station_outlet_temp": station_outlet_temp,
            "station_inlet_temp": station_inlet_temp
        }
    
    async def _check_station_online(self, station_id: str) -> bool:
        """Проверка онлайн статуса станции"""
        try:
            from ocpp_ws_server.redis_manager import redis_manager
            connected_stations = await redis_manager.get_stations()
            return station_id in connected_stations
        except Exception as e:
            logger.warning(f"Не удалось проверить статус станции {station_id}: {e}")
            return False
    
    def _build_status_response(self, session_data: Dict[str, Any], energy_data: Dict[str, Any], 
                              progress: Dict[str, Any], meter_data: Dict[str, Any], 
                              station_online: bool) -> Dict[str, Any]:
        """Построение полного ответа о статусе зарядки"""
        
        # Безопасные преобразования
        def safe_float(value, default=0.0):
            if value is None:
                return default
            try:
                return float(value)
            except (ValueError, TypeError):
                return default
        
        def safe_int(value, default=0):
            if value is None:
                return default
            try:
                return int(value)
            except (ValueError, TypeError):
                return default
        
        # Параметры зарядки
        charging_power = safe_float(meter_data.get('power'), 0.0) / 1000.0  # W → kW
        station_current = safe_float(meter_data.get('current'), 0.0)
        station_voltage = safe_float(meter_data.get('voltage'), 0.0)
        
        # Данные электромобиля  
        ev_battery_soc = safe_int(meter_data.get('soc'), 0)
        ev_current = safe_float(meter_data.get('ev_current'), 0.0)
        ev_voltage = safe_float(meter_data.get('ev_voltage'), 0.0)
        
        # Температуры
        station_body_temp = meter_data.get('station_body_temp', 0)
        if station_body_temp == 0:
            station_body_temp = safe_int(meter_data.get('temperature'), 0)
        
        # Показания счетчика
        meter_start_wh = safe_float(session_data.get('meter_start'), 0.0)
        meter_current_wh = safe_float(meter_data.get('energy_register'), meter_start_wh)
        
        return {
            "success": True,
            "session_id": session_data['session_id'],
            "status": session_data['status'],
            "start_time": session_data['start_time'].isoformat() if session_data['start_time'] else None,
            "stop_time": session_data['stop_time'].isoformat() if session_data['stop_time'] else None,
            "duration_minutes": progress['duration_minutes'],
            
            # Энергетические данные
            "energy_consumed": round(energy_data['actual_energy_consumed'], 3),
            "energy_consumed_kwh": round(energy_data['actual_energy_consumed'], 3),
            "cost": round(energy_data['actual_cost'], 2),
            "final_amount_som": round(energy_data['actual_cost'], 2),
            "amount_charged_som": round(energy_data['actual_cost'], 2),
            "limit_value": round(float(session_data['limit_value']), 2),
            "progress_percent": round(progress['progress_percent'], 1),
            
            # Параметры зарядки
            "charging_power": round(charging_power, 1),
            "station_current": round(station_current, 1),
            "station_voltage": round(station_voltage, 1),
            
            # Данные электромобиля
            "ev_battery_soc": ev_battery_soc,
            "ev_current": round(ev_current, 1),
            "ev_voltage": round(ev_voltage, 1),
            
            # Температурный мониторинг
            "temperatures": {
                "station_body": station_body_temp,
                "station_outlet": meter_data.get('station_outlet_temp', 0),
                "station_inlet": meter_data.get('station_inlet_temp', 0)
            },
            
            # Технические данные
            "meter_start": int(meter_start_wh),
            "meter_current": int(meter_current_wh),
            "station_online": station_online,
            "last_update": meter_data.get('timestamp').isoformat() if meter_data.get('timestamp') else None,
            
            # Обратная совместимость
            "current_energy": round(energy_data['actual_energy_consumed'], 3),
            "current_amount": round(energy_data['actual_cost'], 2),
            "limit_type": session_data['limit_type'],
            "transaction_id": session_data['transaction_id'],
            "ocpp_transaction_id": str(session_data['ocpp_transaction_id']) if session_data['ocpp_transaction_id'] is not None else None,
            "station_id": session_data['station_id'],
            "client_id": session_data['user_id'],
            "rate_per_kwh": float(session_data['price_per_kwh']),
            "ocpp_status": session_data['ocpp_status'],
            "has_meter_data": session_data['meter_start'] is not None,
            
            "message": "Зарядка активна" if session_data['status'] == 'started' 
                      else "Зарядка завершена" if session_data['status'] == 'stopped'
                      else "Ошибка зарядки"
        }
    
    def _safe_float(self, value, default=0.0):
        """Безопасное преобразование в float"""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default
    
    def _safe_int(self, value, default=0):
        """Безопасное преобразование в int"""
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default