"""
Сервисный слой для операций зарядки
"""
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import text
import logging
import json

from app.crud.ocpp_service import payment_service
from app.services.pricing_service import PricingService

logger = logging.getLogger(__name__)

class ChargingService:
    """Сервис для управления сессиями зарядки"""
    
    def __init__(self, db: Session):
        self.db = db
        self.pricing_service = PricingService(db)
    
    async def start_charging_session(
        self,
        client_id: str,
        station_id: str,
        connector_id: int,
        energy_kwh: Optional[float],
        amount_som: Optional[float],
        redis_manager: Any
    ) -> Dict[str, Any]:
        """Начать сессию зарядки с резервированием средств

        Args:
            client_id: UUID клиента
            station_id: ID станции (формат: CHR-BGK-001)
            connector_id: Номер коннектора (1-10)
            energy_kwh: Энергия для зарядки в кВт⋅ч (опционально, должна быть > 0)
            amount_som: Предоплаченная сумма в сомах (опционально, должна быть > 0)
            redis_manager: Redis менеджер для отправки команд

        Returns:
            Dict с результатом запуска сессии

        Raises:
            ValueError: Если параметры некорректны (отрицательные значения, неверный формат)
        """

        # 0. КРИТИЧНАЯ ВАЛИДАЦИЯ: Защита от отрицательных значений и некорректных параметров
        if energy_kwh is not None and energy_kwh <= 0:
            logger.warning(f"⚠️ Попытка начать зарядку с отрицательной энергией: {energy_kwh}")
            return {
                "success": False,
                "error": "invalid_parameters",
                "message": "Энергия должна быть положительным числом"
            }

        if amount_som is not None and amount_som <= 0:
            logger.warning(f"⚠️ Попытка начать зарядку с отрицательной суммой: {amount_som}")
            return {
                "success": False,
                "error": "invalid_parameters",
                "message": "Сумма должна быть положительным числом"
            }

        if amount_som is not None and amount_som > 100000:
            logger.warning(f"⚠️ Попытка начать зарядку с суммой выше лимита: {amount_som}")
            return {
                "success": False,
                "error": "invalid_parameters",
                "message": "Максимальная сумма резервирования: 100,000 сом"
            }

        if connector_id < 1 or connector_id > 10:
            logger.warning(f"⚠️ Попытка начать зарядку с некорректным connector_id: {connector_id}")
            return {
                "success": False,
                "error": "invalid_parameters",
                "message": "Номер коннектора должен быть от 1 до 10"
            }

        # 1. Проверка клиента и баланса (с FOR UPDATE для предотвращения race conditions)
        client = self._validate_client(client_id, for_update=True)
        if not client['success']:
            return client
        
        # 2. Проверка станции и тарифов
        station_info = self._validate_station(station_id, connector_id, client_id)
        if not station_info['success']:
            return station_info
        
        # 3. Расчет стоимости и резервирования
        reservation = self._calculate_reservation(
            client['balance'],
            station_info['pricing_result'],
            energy_kwh,
            amount_som,
            promo_code=None,  # TODO: добавить поддержку промо-кодов в API
            client_id=client_id
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

        # 7. Создание сессии (сначала, чтобы получить session_id для id_tag)
        session_id = self._create_charging_session(
            client_id,
            station_id,
            reservation,
            station_info['pricing_result'],
            energy_kwh,
            amount_som
        )

        # 8. Создание OCPP авторизации с session_id в id_tag (формат Voltera)
        id_tag = self._setup_ocpp_authorization(client_id, session_id)

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
            "pricing": station_info['pricing'],  # Уже конвертирован в dict через to_dict()
            "estimated_cost": reservation['amount'],
            "reserved_amount": reservation['amount'],
            "new_balance": float(new_balance),
            "message": "Зарядка запущена, средства зарезервированы" if station_online else "Сессия создана, средства зарезервированы. Зарядка начнется при подключении станции.",
            "station_online": station_online
        }
    
    def _validate_client(self, client_id: str, for_update: bool = False) -> Dict[str, Any]:
        """Проверка существования клиента и его баланса.

        Args:
            client_id: UUID клиента
            for_update: Если True, блокирует строку для предотвращения race conditions
        """
        # FOR UPDATE блокирует строку до конца транзакции, предотвращая race conditions
        query = "SELECT id, balance, status FROM clients WHERE id = :client_id"
        if for_update:
            query += " FOR UPDATE"

        result = self.db.execute(
            text(query),
            {"client_id": client_id}
        ).fetchone()

        if not result:
            return {
                "success": False,
                "error": "client_not_found",
                "message": "Клиент не найден"
            }

        # Проверка статуса клиента
        client_status = result[2] if len(result) > 2 else None
        if client_status == "pending_deletion":
            return {
                "success": False,
                "error": "account_deletion_pending",
                "message": "Ваш аккаунт находится в процессе удаления. Операции заблокированы."
            }

        if client_status == "blocked":
            return {
                "success": False,
                "error": "account_blocked",
                "message": "Ваш аккаунт заблокирован. Обратитесь в поддержку."
            }

        return {
            "success": True,
            "id": result[0],
            "balance": Decimal(str(result[1])),
            "status": client_status
        }
    
    def _validate_station(self, station_id: str, connector_id: Optional[int] = None, client_id: Optional[str] = None) -> Dict[str, Any]:
        """Проверка станции и получение динамического тарифа"""
        # Проверяем и административный статус (active) и доступность (is_available)
        result = self.db.execute(text("""
            SELECT s.id, s.status, s.is_available, s.last_heartbeat_at,
                   c.connector_type, c.power_kw
            FROM stations s
            LEFT JOIN connectors c ON s.id = c.station_id
                AND c.connector_number = COALESCE(:connector_id, 1)
            WHERE s.id = :station_id AND s.status = 'active'
        """), {"station_id": station_id, "connector_id": connector_id}).fetchone()
        
        if not result:
            return {
                "success": False,
                "error": "station_not_found",
                "message": "Станция не найдена или отключена администратором"
            }
        
        # Проверяем доступность по heartbeat
        if not result[2]:  # is_available = false
            last_heartbeat = result[3]
            if last_heartbeat:
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
        
        # Получаем динамические тарифы через улучшенный PricingService
        try:
            pricing_result = self.pricing_service.calculate_pricing(
                station_id=station_id,
                connector_type=result[4],  # connector_type из таблицы connectors (VARCHAR)
                power_kw=result[5],
                client_id=client_id  # Учитываем клиентские тарифы
            )
            
            return {
                "success": True,
                "id": result[0],
                "status": result[1],
                "pricing_result": pricing_result,
                "pricing": pricing_result.to_dict(),  # Для совместимости
                "connector_type": result[4],
                "power_kw": result[5]
            }
        except Exception as e:
            logger.error(f"Ошибка расчета тарифа для станции {station_id}: {e}")
            # Используем метод базового тарифа из сервиса
            default_pricing = self.pricing_service._get_default_pricing()
            return {
                "success": True,
                "id": result[0],
                "status": result[1],
                "pricing_result": default_pricing,
                "pricing": default_pricing.to_dict()
            }
    
    def _calculate_reservation(
        self,
        balance: Decimal,
        pricing_result,  # Теперь используем PricingResult объект
        energy_kwh: Optional[float],
        amount_som: Optional[float],
        promo_code: Optional[str] = None,
        client_id: Optional[str] = None,
        estimated_duration: int = 60  # Предполагаемая длительность в минутах
    ) -> Dict[str, Any]:
        """Расчет суммы резервирования и лимитов с учетом полного тарифа"""
        
        # Используем новый метод расчета стоимости сессии
        session_cost = None
        if energy_kwh:
            session_cost = self.pricing_service.calculate_session_cost(
                energy_kwh=energy_kwh,
                duration_minutes=estimated_duration,
                pricing=pricing_result,
                promo_code=promo_code,
                client_id=client_id
            )
            estimated_cost = float(session_cost.final_amount)
            base_amount = float(session_cost.base_amount)
            discount_amount = float(session_cost.discount_amount)
        else:
            # Если энергия не указана, делаем примерный расчет
            estimated_cost = float(pricing_result.session_fee)
            if pricing_result.rate_per_minute > 0:
                estimated_cost += float(pricing_result.rate_per_minute * estimated_duration)
            base_amount = estimated_cost
            discount_amount = 0
        
        if energy_kwh and amount_som:
            # Режим 1: Лимит по энергии + максимальная сумма
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
            reservation_amount = (energy_kwh * float(pricing_result.rate_per_kwh)) + float(pricing_result.session_fee)
            if pricing_result.rate_per_minute > 0:
                reservation_amount += estimated_duration * float(pricing_result.rate_per_minute)
            limit_type = 'energy'
            limit_value = energy_kwh

        else:
            # Режим 4: Безлимитная зарядка
            max_reservation = 200.0 + float(pricing_result.session_fee)
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
            "limit_value": limit_value,
            "base_amount": base_amount,
            "discount_amount": discount_amount
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
        """Проверка наличия активной сессии с блокировкой для предотвращения race conditions.

        FOR UPDATE SKIP LOCKED позволяет обнаружить активную сессию даже если другой
        запрос пытается создать сессию одновременно.
        """
        result = self.db.execute(text("""
            SELECT id FROM charging_sessions
            WHERE user_id = :client_id AND status = 'started'
            FOR UPDATE SKIP LOCKED
        """), {"client_id": client_id}).fetchone()

        return result is not None
    
    def _reserve_funds(self, client_id: str, amount: float, station_id: str) -> Decimal:
        """Резервирование средств на балансе"""
        return payment_service.update_client_balance(
            self.db, client_id, Decimal(str(amount)), "subtract",
            f"Резервирование средств для зарядки на станции {station_id}"
        )
    
    def _setup_ocpp_authorization(self, client_id: str, session_id: str) -> str:
        """Создание OCPP авторизации (как Voltera - телефон клиента).

        OCPP 1.6 ограничивает id_tag до 20 символов!
        Voltera использует телефон клиента как id_tag - это ПОСТОЯННЫЙ идентификатор.
        Преимущество: легко найти сессию по id_tag через client -> phone.

        Формат: телефон без + (например: 996555123456) - до 15 символов
        """
        # Получаем телефон клиента из БД
        phone_result = self.db.execute(text("""
            SELECT phone FROM clients WHERE id = :client_id
        """), {"client_id": client_id}).fetchone()

        if phone_result and phone_result[0]:
            # Убираем + и пробелы, оставляем только цифры (до 20 символов)
            phone = ''.join(filter(str.isdigit, phone_result[0]))[:20]
            id_tag = phone
            logger.info(f"🏷️ id_tag = телефон клиента: {id_tag} (как Voltera)")
        else:
            # Fallback: если телефона нет, используем hash от session_id
            import hashlib
            import time
            session_hash = hashlib.md5(session_id.encode()).hexdigest()[:8].upper()
            ts_hex = hex(int(time.time()))[-4:].upper()
            id_tag = f"E{session_hash}{ts_hex}"
            logger.warning(f"⚠️ Телефон не найден для {client_id}, fallback id_tag: {id_tag}")

        # Создаём/обновляем авторизацию
        self.db.execute(text("""
            INSERT INTO ocpp_authorization (id_tag, status, parent_id_tag, client_id)
            VALUES (:id_tag, 'Accepted', NULL, :client_id)
            ON CONFLICT (id_tag) DO UPDATE SET status = 'Accepted', client_id = :client_id
        """), {"id_tag": id_tag, "client_id": client_id})

        return id_tag
    
    def _create_charging_session(
        self,
        client_id: str,
        station_id: str,
        reservation: Dict[str, Any],
        pricing_result,  # PricingResult объект
        energy_kwh: Optional[float],
        amount_som: Optional[float]
    ) -> str:
        """Создание сессии зарядки в БД с сохранением тарифа"""
        
        # Сохраняем историю тарифа сначала
        pricing_history_id = None
        if pricing_result:
            try:
                pricing_history_result = self.db.execute(text("""
                    INSERT INTO pricing_history 
                    (station_id, tariff_plan_id, rule_id, calculation_time,
                     rate_per_kwh, rate_per_minute, session_fee, parking_fee_per_minute,
                     currency, rule_name, rule_details)
                    VALUES (:station_id, :tariff_plan_id, :rule_id, :calculation_time,
                            :rate_per_kwh, :rate_per_minute, :session_fee, :parking_fee,
                            :currency, :rule_name, :rule_details)
                    RETURNING id
                """), {
                    "station_id": station_id,
                    "tariff_plan_id": pricing_result.tariff_plan_id,
                    "rule_id": pricing_result.rule_id,
                    "calculation_time": datetime.now(timezone.utc),
                    "rate_per_kwh": pricing_result.rate_per_kwh,
                    "rate_per_minute": pricing_result.rate_per_minute,
                    "session_fee": pricing_result.session_fee,
                    "parking_fee": pricing_result.parking_fee_per_minute,
                    "currency": pricing_result.currency,
                    "rule_name": pricing_result.active_rule,
                    "rule_details": json.dumps(pricing_result.rule_details)
                }).fetchone()
                
                if pricing_history_result:
                    pricing_history_id = pricing_history_result[0]
            except Exception as e:
                logger.warning(f"Не удалось сохранить историю тарифа: {e}")
        
        # Сохраняем сессию с ссылкой на историю тарифа
        insert_result = self.db.execute(text("""
            INSERT INTO charging_sessions
            (user_id, station_id, start_time, status, limit_type, limit_value,
             amount, pricing_history_id, base_amount, final_amount, reserved_amount, payment_processed)
            VALUES (:user_id, :station_id, :start_time, 'started', :limit_type, :limit_value,
                    :amount, :pricing_history_id, :base_amount, :final_amount, :reserved_amount, FALSE)
            RETURNING id
        """), {
            "user_id": client_id,
            "station_id": station_id,
            "start_time": datetime.now(timezone.utc),
            "limit_type": reservation['limit_type'],
            "limit_value": reservation['limit_value'],
            "amount": reservation['amount'],
            "pricing_history_id": pricing_history_id,
            "base_amount": reservation.get('base_amount', reservation['amount']),
            "final_amount": reservation['amount'],
            "reserved_amount": reservation['amount']
        }).fetchone()

        if not insert_result:
            raise ValueError("Не удалось создать сессию зарядки")

        result = insert_result[0]
        
        # Обновляем ссылку на сессию в истории тарифа
        if pricing_history_id:
            self.db.execute(text("""
                UPDATE pricing_history
                SET session_id = :session_id
                WHERE id = :pricing_history_id
            """), {
                "session_id": result,
                "pricing_history_id": pricing_history_id
            })
        
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
        """Отправка команды запуска на станцию через Redis (как Voltera).

        ВАЖНО: Команда публикуется ТОЛЬКО если станция онлайн!
        Сохраняет pending session в Redis для связывания при StartTransaction.
        OCPP 1.6 ограничивает id_tag до 20 символов.
        """
        # === ШАГА 1: Проверяем онлайн-статус через TTL ключ ===
        is_online = await redis_manager.is_station_online(station_id)

        # === ШАГ 2: Сохраняем pending session в Redis (всегда) ===
        # При StartTransaction ws_handler найдёт session_id по station_id:connector_id
        pending_key = f"pending:{station_id}:{connector_id}"
        await redis_manager.redis.setex(pending_key, 300, session_id)  # TTL 5 минут
        logger.info(f"📝 Сохранён pending session: {pending_key} -> {session_id}")

        # Также сохраняем маппинг id_tag -> session_id (для надёжности)
        idtag_key = f"idtag:{id_tag}"
        await redis_manager.redis.setex(idtag_key, 86400, session_id)  # TTL 24 часа
        logger.info(f"📝 Сохранён маппинг id_tag: {idtag_key} -> {session_id}")

        # === ШАГ 3: Публикуем команду ТОЛЬКО если станция онлайн (как Voltera) ===
        if is_online:
            command_data = {
                "action": "RemoteStartTransaction",
                "connector_id": connector_id,
                "id_tag": id_tag,
                "session_id": session_id,
                "limit_type": limit_type,
                "limit_value": limit_value
            }

            # Публикуем без retry (как Voltera)
            subscribers = await redis_manager.publish_command(station_id, command_data)

            if subscribers > 0:
                logger.info(
                    f"✅ Команда запуска ДОСТАВЛЕНА на станцию {station_id} "
                    f"(subscribers={subscribers}, id_tag={id_tag})"
                )
            else:
                logger.error(
                    f"❌ 0 ПОДПИСЧИКОВ для {station_id}! "
                    f"Станция онлайн по TTL, но pubsub не готов. "
                    f"Сессия создана, команда будет отправлена при переподключении."
                )
        else:
            logger.warning(
                f"⚠️ Станция {station_id} ОФЛАЙН - команда НЕ публикуется (как Voltera). "
                f"Pending session сохранён, зарядка начнётся при подключении станции."
            )

        return is_online
    
    async def stop_charging_session(
        self,
        session_id: str,
        client_id: str,
        redis_manager: Any
    ) -> Dict[str, Any]:
        """Остановить сессию зарядки с расчетом и возвратом средств

        Args:
            session_id: ID сессии зарядки для остановки
            client_id: ID клиента, запрашивающего остановку (для проверки владельца)
            redis_manager: Redis менеджер для отправки команд на станцию

        Returns:
            Dict с результатом остановки сессии

        Raises:
            HTTPException: Если клиент не является владельцем сессии (403)
        """

        # 1. Получение информации о сессии
        session_info = self._get_session_info(session_id)
        if not session_info:
            return {
                "success": False,
                "error": "session_not_found",
                "message": "Активная сессия зарядки не найдена"
            }

        # 2. КРИТИЧНАЯ ПРОВЕРКА: Клиент должен быть владельцем сессии
        if session_info['client_id'] != client_id:
            logger.warning(
                f"⚠️ Попытка остановить чужую сессию: "
                f"session_id={session_id}, owner={session_info['client_id']}, "
                f"requester={client_id}"
            )
            return {
                "success": False,
                "error": "access_denied",
                "message": "У вас нет прав для остановки этой сессии"
            }
        
        # 3. Расчет фактического потребления
        actual_energy = self._get_actual_energy_consumed(session_id, session_info.get('energy'))
        
        # 4. Расчет стоимости
        rate_per_kwh = self._get_session_rate(session_info)
        actual_cost = Decimal(str(actual_energy * rate_per_kwh))
        reserved_amount = Decimal(str(session_info['reserved_amount']))

        # 5. Обработка превышения резерва или возврата
        refund_amount, additional_charge = self._calculate_refund_or_charge(
            session_info['client_id'],
            actual_cost,
            reserved_amount,
            session_id
        )

        # 6. Обновление баланса
        new_balance = self._process_session_payment(
            session_info['client_id'],
            refund_amount,
            additional_charge,
            session_id,
            actual_energy
        )

        # 7. Обновление сессии в БД
        self._finalize_session(session_id, actual_energy, float(actual_cost))

        # 8. Освобождение коннектора
        self._update_connector_status(session_info['station_id'], 1, 'available')

        # 9. Отправка команды остановки
        station_online = await self._send_stop_command(
            redis_manager,
            session_info['station_id'],
            session_id
        )

        # 10. Коммит транзакции
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
    
    def _get_session_info(self, session_id: str, for_update: bool = True) -> Optional[Dict[str, Any]]:
        """Получение информации о сессии.

        Args:
            session_id: ID сессии
            for_update: Если True (по умолчанию), блокирует сессию для предотвращения
                       race conditions при параллельных запросах на остановку
        """
        # FOR UPDATE блокирует строку сессии до конца транзакции
        lock_clause = "FOR UPDATE" if for_update else ""

        result = self.db.execute(text(f"""
            SELECT cs.id, cs.user_id, cs.station_id, cs.start_time, cs.status,
                   cs.limit_value, cs.reserved_amount, cs.energy, s.price_per_kwh,
                   tp.id as tariff_plan_id, cs.payment_processed
            FROM charging_sessions cs
            LEFT JOIN stations s ON cs.station_id = s.id
            LEFT JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE cs.id = :session_id AND cs.status = 'started'
            {lock_clause}
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
            'tariff_plan_id': result[9],
            'payment_processed': result[10] or False
        }
    
    def _get_actual_energy_consumed(self, session_id: str, session_energy: Optional[float]) -> float:
        """Получение фактически потребленной энергии

        Приоритет источников данных:
        1. charging_sessions.energy (если > 0)
        2. ocpp_meter_values.energy_active_import_register - meter_start (последние показания)
        3. ocpp_transactions.meter_stop - meter_start (если станция прислала StopTransaction)
        4. 0.0 (fallback)
        """
        # 1. Если в сессии уже есть энергия > 0, используем её
        if session_energy and float(session_energy) > 0:
            return float(session_energy)

        # 2. Получаем энергию из последних meter_values (приоритет) или из транзакции
        result = self.db.execute(text("""
            SELECT COALESCE(
                -- Приоритет 1: последние показания счётчика из meter_values
                (mv.energy_active_import_register - ot.meter_start) / 1000.0,
                -- Приоритет 2: данные из завершённой транзакции
                (ot.meter_stop - ot.meter_start) / 1000.0,
                -- Fallback
                0
            ) as energy_kwh
            FROM ocpp_transactions ot
            LEFT JOIN LATERAL (
                SELECT energy_active_import_register
                FROM ocpp_meter_values
                WHERE ocpp_transaction_id = ot.id
                ORDER BY timestamp DESC
                LIMIT 1
            ) mv ON true
            WHERE ot.charging_session_id = :session_id
            ORDER BY ot.created_at DESC
            LIMIT 1
        """), {"session_id": session_id}).fetchone()

        energy = float(result[0]) if result and result[0] else 0.0
        logger.info(f"📊 Фактическое потребление для сессии {session_id}: {energy:.3f} кВт⋅ч")
        return energy
    
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
        
        return 13.5  # Default rate
    
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
                energy = :actual_energy, amount = :actual_cost,
                payment_processed = TRUE
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
        """Отправка команды остановки на станцию (по подходу Voltera)"""
        connected_stations = await redis_manager.get_stations()
        is_online = station_id in connected_stations

        if not is_online:
            logger.warning(f"⚠️ Станция {station_id} offline - RemoteStopTransaction не отправлен")
            return False

        # Получаем OCPP transaction_id (БЕЗ фильтра по status, как в Voltera)
        result = self.db.execute(text("""
            SELECT transaction_id FROM ocpp_transactions
            WHERE charging_session_id = :session_id
            ORDER BY created_at DESC LIMIT 1
        """), {"session_id": session_id}).fetchone()

        if result and result[0]:
            command_data = {
                "action": "RemoteStopTransaction",
                "transaction_id": result[0]
            }
            subscribers = await redis_manager.publish_command(station_id, command_data)
            if subscribers > 0:
                logger.info(f"✅ RemoteStopTransaction отправлен: station={station_id}, transaction_id={result[0]}, subscribers={subscribers}")
            else:
                logger.error(f"❌ RemoteStopTransaction НЕ ДОСТАВЛЕН на станцию {station_id}! 0 подписчиков в Redis")
        else:
            logger.warning(f"⚠️ OCPP транзакция не найдена для сессии {session_id} - RemoteStopTransaction не отправлен")

        return is_online
    
    async def get_charging_status(self, session_id: str) -> Dict[str, Any]:
        """Получить полный статус сессии зарядки с OCPP данными (по подходу Voltera)"""

        logger.info(f"📊 Запрос статуса зарядки для сессии: {session_id}")

        try:
            # SQL запрос по подходу Voltera с LATERAL JOIN для получения последних meter_values
            session_query = text("""
                SELECT
                    cs.id as session_id,
                    cs.user_id,
                    cs.station_id,
                    cs.start_time,
                    cs.stop_time,
                    cs.energy as session_energy,
                    cs.amount,
                    cs.reserved_amount,
                    cs.status,
                    cs.transaction_id,
                    cs.limit_type,
                    cs.limit_value,
                    s.price_per_kwh,
                    s.session_fee,

                    -- Данные транзакции
                    ot.id as ocpp_transaction_id,
                    ot.transaction_id as ocpp_tx_id,
                    ot.meter_start,
                    ot.meter_stop,
                    ot.status as ocpp_status,

                    -- Последние meter values через LATERAL
                    mv.energy_active_import_register as current_meter,
                    mv.power_active_import as power_w,
                    mv.current_import,
                    mv.voltage,
                    mv.soc as ev_battery_soc,
                    mv.timestamp as meter_timestamp,

                    -- Вычисленная энергия: приоритет cs.energy (если > 0), fallback на meter_values
                    -- NULLIF превращает 0 в NULL, чтобы COALESCE перешёл к следующему значению
                    COALESCE(
                        NULLIF(cs.energy, 0),
                        (mv.energy_active_import_register - ot.meter_start) / 1000.0,
                        0
                    ) as energy_kwh

                FROM charging_sessions cs
                LEFT JOIN stations s ON cs.station_id = s.id
                LEFT JOIN ocpp_transactions ot ON cs.id = ot.charging_session_id
                LEFT JOIN LATERAL (
                    SELECT * FROM ocpp_meter_values
                    WHERE ocpp_transaction_id = ot.id
                    ORDER BY timestamp DESC
                    LIMIT 1
                ) mv ON true
                WHERE cs.id = :session_id
            """)

            session_result = self.db.execute(session_query, {"session_id": session_id})
            row = session_result.fetchone()

            if not row:
                logger.warning(f"Сессия {session_id} не найдена в БД")
                return {
                    "success": False,
                    "error": "session_not_found",
                    "message": "Сессия зарядки не найдена"
                }

            # Распаковываем результат
            (
                session_id_db, user_id, station_id, start_time, stop_time,
                session_energy, amount, reserved_amount, status, transaction_id,
                limit_type, limit_value, price_per_kwh, session_fee,
                ocpp_transaction_id, ocpp_tx_id, meter_start, meter_stop, ocpp_status,
                current_meter, power_w, current_import, voltage, ev_battery_soc, meter_timestamp,
                energy_kwh
            ) = row

            # Безопасные преобразования
            energy_kwh = float(energy_kwh) if energy_kwh else 0.0
            price_per_kwh = float(price_per_kwh) if price_per_kwh else 13.5
            session_fee = float(session_fee) if session_fee else 0.0
            reserved_amount = float(reserved_amount) if reserved_amount else 0.0
            limit_value = float(limit_value) if limit_value else 0.0
            power_kw = float(power_w) / 1000.0 if power_w else 0.0

            # Вычисляем стоимость
            current_amount = energy_kwh * price_per_kwh

            # Вычисляем прогресс
            progress_percent = 0.0
            if limit_type == "energy" and limit_value > 0:
                progress_percent = min(100, (energy_kwh / limit_value) * 100)
            elif limit_type == "amount" and limit_value > 0:
                progress_percent = min(100, (current_amount / limit_value) * 100)

            # Вычисляем длительность
            duration_seconds = 0
            if start_time:
                end_time = stop_time or datetime.now(timezone.utc)
                duration_seconds = int((end_time - start_time).total_seconds())

            # Проверка статуса станции онлайн
            station_online = await self._check_station_online(station_id)

            logger.info(f"✅ Статус получен: energy={energy_kwh:.3f} кВт⋅ч, power={power_kw:.1f} кВт, online={station_online}")

            return {
                "success": True,
                "session": {
                    "id": session_id_db,
                    "session_id": session_id_db,
                    "status": status or "preparing",
                    "station_id": station_id,
                    "connector_id": 1,  # TODO: получить из транзакции
                    "ocpp_transaction_id": ocpp_transaction_id,

                    # Энергетические данные
                    "energy_consumed": round(energy_kwh, 3),
                    "energy_kwh": round(energy_kwh, 3),
                    "current_cost": round(current_amount, 2),
                    "current_amount": round(current_amount, 2),
                    "power_kw": round(power_kw, 2),

                    # Резерв и тарифы
                    "reserved_amount": round(reserved_amount, 2),
                    "rate_per_kwh": round(price_per_kwh, 2),
                    "session_fee": round(session_fee, 2),

                    # Длительность
                    "charging_duration_minutes": duration_seconds // 60,
                    "duration_seconds": duration_seconds,

                    # Лимиты и прогресс
                    "limit_type": limit_type or "none",
                    "limit_value": round(limit_value, 2),
                    "limit_reached": progress_percent >= 100,
                    "limit_percentage": round(progress_percent, 1),
                    "progress_percent": round(progress_percent, 1),

                    # Показания счётчика
                    "meter_start": float(meter_start) if meter_start else 0,
                    "meter_current": float(current_meter) if current_meter else 0,

                    # Данные EV (если есть)
                    "ev_battery_soc": int(ev_battery_soc) if ev_battery_soc else None,

                    # Статус станции
                    "station_online": station_online,

                    # Timestamps
                    "start_time": start_time.isoformat() if start_time else None,
                    "stop_time": stop_time.isoformat() if stop_time else None,
                }
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
                "price_per_kwh": float(session[15]) if session[15] is not None else 13.5
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
            # Метод 1: Через charging_session_id в OCPP транзакции
            latest_meter_query = text("""
                SELECT mv.energy_active_import_register
                FROM ocpp_meter_values mv
                JOIN ocpp_transactions ot ON mv.ocpp_transaction_id = ot.id
                WHERE ot.charging_session_id = :session_id
                AND mv.energy_active_import_register IS NOT NULL
                ORDER BY mv.timestamp DESC LIMIT 1
            """)
            latest_result = self.db.execute(latest_meter_query, {"session_id": session_id})
            latest_meter = latest_result.fetchone()

            # Метод 2: Если не нашли через charging_session_id, ищем через transaction_id
            if not latest_meter or not latest_meter[0]:
                transaction_id = session_data.get('transaction_id')
                if transaction_id:
                    logger.debug(f"📊 Поиск meter values через transaction_id: {transaction_id}")
                    fallback_query = text("""
                        SELECT mv.energy_active_import_register
                        FROM ocpp_meter_values mv
                        JOIN ocpp_transactions ot ON mv.ocpp_transaction_id = ot.id
                        WHERE ot.transaction_id = :transaction_id
                        AND mv.energy_active_import_register IS NOT NULL
                        ORDER BY mv.timestamp DESC LIMIT 1
                    """)
                    latest_result = self.db.execute(fallback_query, {"transaction_id": int(transaction_id)})
                    latest_meter = latest_result.fetchone()

            if latest_meter and latest_meter[0]:
                current_meter = float(latest_meter[0])
                ocpp_energy_wh = current_meter - float(session_data['meter_start'])
                actual_energy_consumed = max(ocpp_energy_wh / 1000.0, actual_energy_consumed)
                actual_cost = actual_energy_consumed * float(session_data['price_per_kwh'])
                logger.debug(f"📊 Energy calculated: current={current_meter}, start={session_data['meter_start']}, consumed={actual_energy_consumed} kWh")
        
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

    async def check_and_stop_hanging_sessions(self, redis_manager: Any, max_hours: int = 12, connection_timeout_minutes: int = 10) -> Dict[str, Any]:
        """
        Автоматически останавливает зависшие сессии зарядки

        Проверяет два типа зависших сессий:
        1. Сессии длительностью > max_hours (по умолчанию 12 часов)
        2. Сессии без OCPP transaction > connection_timeout_minutes (по умолчанию 10 минут)
           - Пользователь нажал "начать зарядку", но не подключил кабель

        Args:
            redis_manager: Redis менеджер для отправки команд
            max_hours: Максимальное время зарядки в часах (по умолчанию 12)
            connection_timeout_minutes: Таймаут на подключение кабеля в минутах (по умолчанию 10)

        Returns:
            Dict с информацией о завершенных сессиях
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_hours)
        connection_timeout = datetime.now(timezone.utc) - timedelta(minutes=connection_timeout_minutes)

        # ПРОВЕРКА 1: Сессии длительностью > max_hours
        long_sessions_query = text("""
            SELECT id, user_id, station_id, start_time, amount
            FROM charging_sessions
            WHERE status = 'started'
            AND start_time < :cutoff_time
            ORDER BY start_time ASC
        """)

        # ПРОВЕРКА 2: Сессии без OCPP транзакции (кабель не подключен)
        no_transaction_query = text("""
            SELECT cs.id, cs.user_id, cs.station_id, cs.start_time, cs.amount
            FROM charging_sessions cs
            LEFT JOIN ocpp_transactions ot ON cs.id = ot.charging_session_id
            WHERE cs.status = 'started'
            AND cs.start_time < :connection_timeout
            AND ot.id IS NULL
            ORDER BY cs.start_time ASC
        """)

        # Выполняем обе проверки
        long_result = self.db.execute(long_sessions_query, {"cutoff_time": cutoff_time})
        long_sessions = long_result.fetchall()

        no_transaction_result = self.db.execute(no_transaction_query, {"connection_timeout": connection_timeout})
        no_transaction_sessions = no_transaction_result.fetchall()

        # Объединяем результаты (используем set для удаления дубликатов по session_id)
        all_hanging_sessions = {}
        for session in long_sessions:
            all_hanging_sessions[session[0]] = ("long_duration", session)
        for session in no_transaction_sessions:
            if session[0] not in all_hanging_sessions:
                all_hanging_sessions[session[0]] = ("no_connection", session)

        if not all_hanging_sessions:
            logger.info(f"✅ Зависших сессий не найдено (проверка: {max_hours}ч активных, {connection_timeout_minutes}мин без подключения)")
            return {
                "success": True,
                "stopped_count": 0,
                "sessions": [],
                "long_sessions": 0,
                "no_connection_sessions": 0
            }

        logger.warning(f"⚠️ Найдено зависших сессий: {len(long_sessions)} длинных, {len(no_transaction_sessions)} без подключения")

        stopped_sessions = []
        errors = []

        for sess_id, (reason, session) in all_hanging_sessions.items():
            session_id = session[0]
            client_id = session[1]
            station_id = session[2]
            start_time = session[3]
            reserved_amount = session[4]

            duration_hours = (datetime.now(timezone.utc) - start_time).total_seconds() / 3600
            duration_minutes = duration_hours * 60

            try:
                if reason == "no_connection":
                    logger.warning(
                        f"⚠️ ЗАВИСШАЯ СЕССИЯ (НЕТ ПОДКЛЮЧЕНИЯ): session_id={session_id}, "
                        f"client={client_id}, время с создания={duration_minutes:.0f}мин, резерв={reserved_amount} сом"
                    )
                else:
                    logger.warning(
                        f"⚠️ ЗАВИСШАЯ СЕССИЯ (СЛИШКОМ ДОЛГО): session_id={session_id}, "
                        f"client={client_id}, длительность={duration_hours:.1f}ч"
                    )

                # Останавливаем сессию с автоматическим расчетом
                # Передаем client_id владельца для авторизации операции
                stop_result = await self.stop_charging_session(session_id, client_id, redis_manager)

                if stop_result.get("success"):
                    stopped_sessions.append({
                        "session_id": session_id,
                        "client_id": client_id,
                        "station_id": station_id,
                        "reason": reason,
                        "duration_hours": round(duration_hours, 1),
                        "duration_minutes": round(duration_minutes, 0),
                        "energy_consumed": stop_result.get("energy_consumed", 0),
                        "actual_cost": stop_result.get("actual_cost", 0),
                        "refund_amount": stop_result.get("refund_amount", 0)
                    })
                    if reason == "no_connection":
                        logger.info(
                            f"✅ Зависшая сессия {session_id} остановлена (НЕТ ПОДКЛЮЧЕНИЯ за {duration_minutes:.0f}мин). "
                            f"Возврат: {stop_result.get('refund_amount', 0)} сом"
                        )
                    else:
                        logger.info(
                            f"✅ Зависшая сессия {session_id} остановлена (СЛИШКОМ ДОЛГО: {duration_hours:.1f}ч). "
                            f"Потреблено: {stop_result.get('energy_consumed', 0)} кВт⋅ч"
                        )
                else:
                    errors.append({
                        "session_id": session_id,
                        "error": stop_result.get("error", "unknown_error"),
                        "message": stop_result.get("message", "Неизвестная ошибка")
                    })
                    logger.error(f"❌ Не удалось остановить зависшую сессию {session_id}: {stop_result.get('message')}")

            except Exception as e:
                logger.error(f"❌ Критическая ошибка при остановке зависшей сессии {session_id}: {e}", exc_info=True)
                errors.append({
                    "session_id": session_id,
                    "error": "exception",
                    "message": str(e)
                })

        logger.info(
            f"🔄 Проверка зависших сессий завершена: "
            f"найдено={len(all_hanging_sessions)} ({len(long_sessions)} длинных, {len(no_transaction_sessions)} без подключения), "
            f"остановлено={len(stopped_sessions)}, ошибок={len(errors)}"
        )

        return {
            "success": True,
            "stopped_count": len(stopped_sessions),
            "error_count": len(errors),
            "sessions": stopped_sessions,
            "errors": errors,
            "max_hours": max_hours,
            "connection_timeout_minutes": connection_timeout_minutes,
            "long_sessions_found": len(long_sessions),
            "no_connection_sessions_found": len(no_transaction_sessions)
        }