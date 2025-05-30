"""
OCPP 1.6 WebSocket Handler - Полная реализация по best practice
Поддерживаемые сообщения: Tier 1, 2, 3 (все стандартные OCPP 1.6)
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import WebSocket, WebSocketDisconnect
from ocpp.v16 import ChargePoint as CP
from ocpp.routing import on
from ocpp.v16 import call_result, call
from ocpp.v16.enums import (
    RegistrationStatus, AuthorizationStatus, ConfigurationStatus,
    ResetStatus, ResetType, UnlockStatus, AvailabilityStatus,
    AvailabilityType, ClearCacheStatus, UpdateStatus,
    DiagnosticsStatus, FirmwareStatus, TriggerMessageStatus,
    MessageTrigger, UpdateType
)

from .redis_manager import redis_manager
from app.db.session import get_db
from app.crud.ocpp_service import (
    OCPPStationService,
    OCPPTransactionService,
    OCPPMeterService,
    OCPPAuthorizationService,
    OCPPConfigurationService
)
from app.db.models.ocpp import OCPPTransaction
from sqlalchemy import text
from decimal import Decimal

logger = logging.getLogger(__name__)

# Активные сессии для мониторинга лимитов
active_sessions: Dict[str, Dict[str, Any]] = {}

class OCPPChargePoint(CP):
    """
    Расширенный OCPP 1.6 ChargePoint с поддержкой всех стандартных сообщений
    """
    
    def __init__(self, id: str, connection):
        super().__init__(id, connection)
        self.logger = logging.getLogger(f"OCPP.{id}")
        
    # ============================================================================
    # TIER 1: КРИТИЧЕСКИ ВАЖНЫЕ (обязательные для сертификации)
    # ============================================================================
    
    @on('BootNotification')
    def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        """Регистрация станции при запуске"""
        self.logger.info(f"BootNotification: {charge_point_model}, {charge_point_vendor}")
        
        try:
            with next(get_db()) as db:
                # Сохраняем информацию о станции
                firmware_version = kwargs.get('firmware_version')
                OCPPStationService.mark_boot_notification_sent(
                    db, self.id, firmware_version
                )
                
                # Базовая конфигурация
                OCPPConfigurationService.set_configuration(
                    db, self.id, "HeartbeatInterval", "300", readonly=True
                )
                OCPPConfigurationService.set_configuration(
                    db, self.id, "MeterValueSampleInterval", "60", readonly=True
                )
                
                # 🆕 АВТОЗАПУСК: Проверяем pending сессии
                pending_sessions_query = text("""
                    SELECT id, user_id, limit_value, limit_type
                    FROM charging_sessions 
                    WHERE station_id = :station_id 
                    AND status = 'started' 
                    AND transaction_id IS NULL
                """)
                
                pending_sessions = db.execute(pending_sessions_query, {"station_id": self.id}).fetchall()
                
                # Отправляем команды автозапуска для каждой pending сессии
                for session in pending_sessions:
                    session_id, user_id, limit_value, limit_type = session
                    
                    # 🆕 ИСПРАВЛЕНИЕ: Получаем номер телефона клиента вместо CLIENT_ префикса
                    phone_query = text("""
                        SELECT phone FROM clients WHERE id = :client_id
                    """)
                    phone_result = db.execute(phone_query, {"client_id": user_id}).fetchone()
                    id_tag = phone_result[0] if phone_result else f"CLIENT_{user_id}"
                    
                    # Определяем коннектор из занятых коннекторов
                    connector_query = text("""
                        SELECT connector_number FROM connectors 
                        WHERE station_id = :station_id AND status = 'occupied'
                        LIMIT 1
                    """)
                    connector_result = db.execute(connector_query, {"station_id": self.id}).fetchone()
                    connector_id = connector_result[0] if connector_result else 1
                    
                    # Отправляем команду автозапуска через Redis
                    command_data = {
                        "action": "RemoteStartTransaction",
                        "connector_id": connector_id,
                        "id_tag": id_tag,
                        "session_id": session_id,
                        "limit_type": limit_type,
                        "limit_value": limit_value
                    }
                    
                    # Используем asyncio для отправки Redis команды
                    asyncio.create_task(
                        redis_manager.publish_command(self.id, command_data)
                    )
                    
                    self.logger.info(f"🚀 Автозапуск зарядки для сессии {session_id}")
                
            return call_result.BootNotification(
                current_time=datetime.utcnow().isoformat() + 'Z',
                interval=300,
                status=RegistrationStatus.accepted
            )
            
        except Exception as e:
            self.logger.error(f"Error in BootNotification: {e}")
            return call_result.BootNotification(
                current_time=datetime.utcnow().isoformat() + 'Z',
                interval=300,
                status=RegistrationStatus.rejected
            )

    @on('Heartbeat')
    def on_heartbeat(self, **kwargs):
        """Периодические сигналы жизни"""
        self.logger.debug(f"Heartbeat from {self.id}")
        
        try:
            with next(get_db()) as db:
                OCPPStationService.update_heartbeat(db, self.id)
                
            return call_result.Heartbeat(
                current_time=datetime.utcnow().isoformat() + 'Z'
            )
            
        except Exception as e:
            self.logger.error(f"Error in Heartbeat: {e}")
            return call_result.Heartbeat(
                current_time=datetime.utcnow().isoformat() + 'Z'
            )

    @on('StatusNotification')
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        """Изменения статуса коннекторов"""
        self.logger.info(f"StatusNotification: connector={connector_id}, status={status}, error={error_code}")
        
        try:
            with next(get_db()) as db:
                info = kwargs.get('info')
                vendor_id = kwargs.get('vendor_id')
                vendor_error_code = kwargs.get('vendor_error_code')
                
                # Обновляем статус станции (старая логика для совместимости)
                station_status = OCPPStationService.update_station_status(
                    db, self.id, status, error_code, info, vendor_id, vendor_error_code
                )
                
                # Обновляем статус коннектора в JSON поле (старая логика)
                connector_status = station_status.connector_status or []
                
                # Находим или создаем статус для коннектора
                connector_found = False
                for i, conn in enumerate(connector_status):
                    if conn.get('connector_id') == connector_id:
                        connector_status[i] = {
                            'connector_id': connector_id,
                            'status': status,
                            'error_code': error_code,
                            'info': info,
                            'timestamp': datetime.utcnow().isoformat()
                        }
                        connector_found = True
                        break
                
                if not connector_found:
                    connector_status.append({
                        'connector_id': connector_id,
                        'status': status,
                        'error_code': error_code,
                        'info': info,
                        'timestamp': datetime.utcnow().isoformat()
                    })
                
                station_status.connector_status = connector_status
                
                # 🆕 НОВАЯ ЛОГИКА: Обновляем таблицу connectors
                # Конвертируем OCPP статус в наш формат
                connector_status_mapping = {
                    'Available': 'Available',
                    'Preparing': 'Occupied', 
                    'Charging': 'Occupied',
                    'SuspendedEVSE': 'Occupied',
                    'SuspendedEV': 'Occupied',
                    'Finishing': 'Occupied',
                    'Reserved': 'Occupied',
                    'Unavailable': 'Unavailable',
                    'Faulted': 'Faulted'
                }
                
                new_status = connector_status_mapping.get(status, 'Unavailable')
                
                # Обновляем запись в таблице connectors
                update_query = text("""
                    UPDATE connectors 
                    SET status = :status, error_code = :error_code, last_status_update = NOW()
                    WHERE station_id = :station_id AND connector_number = :connector_id
                """)
                db.execute(update_query, {
                    "status": new_status.lower(), 
                    "error_code": error_code, 
                    "station_id": self.id, 
                    "connector_id": connector_id
                })
                
                db.commit()
                
            return call_result.StatusNotification()
            
        except Exception as e:
            self.logger.error(f"Error in StatusNotification: {e}")
            return call_result.StatusNotification()

    @on('Authorize')
    def on_authorize(self, id_tag, **kwargs):
        """Авторизация RFID карт"""
        self.logger.info(f"Authorize request for id_tag: {id_tag}")
        
        try:
            with next(get_db()) as db:
                # Проверяем авторизацию через сервис
                auth_result = OCPPAuthorizationService.authorize_id_tag(db, id_tag)
                
            self.logger.info(f"Authorization result for {id_tag}: {auth_result['status']}")
            
            return call_result.Authorize(id_tag_info=auth_result)
            
        except Exception as e:
            self.logger.error(f"Error in Authorize: {e}")
            return call_result.Authorize(
                id_tag_info={"status": AuthorizationStatus.invalid}
            )

    @on('StartTransaction')
    def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        """Начало сеанса зарядки"""
        self.logger.info(f"StartTransaction: connector={connector_id}, id_tag={id_tag}, meter_start={meter_start}")
        
        try:
            with next(get_db()) as db:
                # Проверяем авторизацию
                auth_result = OCPPAuthorizationService.authorize_id_tag(db, id_tag)
                if auth_result["status"] != "Accepted":
                    self.logger.warning(f"Unauthorized id_tag: {id_tag}")
                    return call_result.StartTransaction(
                        transaction_id=0,
                        id_tag_info=auth_result
                    )
                
                # Генерируем transaction_id
                transaction_id = int(datetime.utcnow().timestamp())
                
                # Создаем транзакцию
                transaction = OCPPTransactionService.start_transaction(
                    db, self.id, transaction_id, connector_id, id_tag,
                    float(meter_start), datetime.fromisoformat(timestamp.replace('Z', ''))
                )
                
                # 🆕 УЛУЧШЕННОЕ СВЯЗЫВАНИЕ: Находим клиента по номеру телефона
                charging_session_id = None
                client_id = None
                
                # Поиск клиента по номеру телефона (idTag)
                client_query = text("""
                    SELECT id FROM clients 
                    WHERE phone = :phone 
                    LIMIT 1
                """)
                client_result = db.execute(client_query, {"phone": id_tag})
                client_row = client_result.fetchone()
                
                if client_row:
                    client_id = client_row[0]
                    self.logger.info(f"🔍 НАЙДЕН КЛИЕНТ: phone={id_tag} -> client_id={client_id}")
                    
                    # Ищем активную мобильную сессию для клиента
                    find_session_query = text("""
                        SELECT id FROM charging_sessions 
                        WHERE user_id = :client_id AND status = 'started' 
                        ORDER BY start_time DESC LIMIT 1
                    """)
                    session_result = db.execute(find_session_query, {"client_id": client_id})
                    session_row = session_result.fetchone()
                    
                    if session_row:
                        charging_session_id = session_row[0]
                        self.logger.info(f"🔗 НАЙДЕНА АКТИВНАЯ СЕССИЯ: {charging_session_id}")
                        
                        # Связываем OCPP транзакцию с мобильной сессией
                        link_query = text("""
                            UPDATE ocpp_transactions 
                            SET charging_session_id = :session_id 
                            WHERE transaction_id = :transaction_id
                        """)
                        db.execute(link_query, {
                            "session_id": charging_session_id,
                            "transaction_id": transaction_id
                        })
                        
                        # Обновляем мобильную сессию с OCPP данными
                        update_session_query = text("""
                            UPDATE charging_sessions 
                            SET transaction_id = :transaction_id 
                            WHERE id = :session_id
                        """)
                        db.execute(update_session_query, {
                            "transaction_id": transaction_id,
                            "session_id": charging_session_id
                        })
                        
                        self.logger.info(f"✅ СВЯЗЫВАНИЕ ЗАВЕРШЕНО: OCPP {transaction_id} ↔ Mobile {charging_session_id}")
                    else:
                        self.logger.warning(f"⚠️ Активная мобильная сессия для клиента {client_id} не найдена")
                else:
                    self.logger.warning(f"⚠️ Клиент с номером {id_tag} не найден")
                
                # Сохраняем в активные сессии с улучшенными данными
                active_sessions[self.id] = {
                    'transaction_id': transaction_id,
                    'charging_session_id': charging_session_id,
                    'meter_start': meter_start,
                    'energy_delivered': 0.0,
                    'connector_id': connector_id,
                    'id_tag': id_tag,
                    'client_id': client_id
                }
                
                # 🆕 АВТОМАТИЧЕСКОЕ ОБНОВЛЕНИЕ: Коннектор становится занят
                update_query = text("""
                    UPDATE connectors 
                    SET status = 'occupied', last_status_update = NOW()
                    WHERE station_id = :station_id AND connector_number = :connector_id
                """)
                db.execute(update_query, {"station_id": self.id, "connector_id": connector_id})
                db.commit()
                
            self.logger.info(f"Transaction started: {transaction_id}, connector {connector_id} marked as Occupied")
            return call_result.StartTransaction(
                transaction_id=transaction_id,
                id_tag_info={"status": AuthorizationStatus.accepted}
            )
            
        except Exception as e:
            self.logger.error(f"Error in StartTransaction: {e}")
            return call_result.StartTransaction(
                transaction_id=0,
                id_tag_info={"status": AuthorizationStatus.invalid}
            )

    @on('StopTransaction')
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        """Завершение сеанса зарядки"""
        id_tag = kwargs.get('id_tag')
        reason = kwargs.get('reason', 'Local')
        
        self.logger.info(f"StopTransaction: transaction_id={transaction_id}, meter_stop={meter_stop}")
        
        try:
            with next(get_db()) as db:
                # Получаем информацию о транзакции для определения коннектора
                transaction = db.query(OCPPTransaction).filter(
                    OCPPTransaction.station_id == self.id,
                    OCPPTransaction.transaction_id == transaction_id
                ).first()
                
                connector_id = transaction.connector_id if transaction else None
                
                # Завершаем транзакцию
                transaction = OCPPTransactionService.stop_transaction(
                    db, self.id, transaction_id, float(meter_stop),
                    datetime.fromisoformat(timestamp.replace('Z', '')), reason
                )
                
                # 🆕 АВТОМАТИЧЕСКОЕ ЗАВЕРШЕНИЕ МОБИЛЬНОЙ СЕССИИ
                if transaction and transaction.charging_session_id:
                    session_id = transaction.charging_session_id
                    try:
                        # Рассчитываем потребленную энергию
                        energy_consumed = (float(meter_stop) - float(transaction.meter_start)) / 1000.0  # Wh → kWh
                        
                        # Получаем тариф
                        tariff_query = text("""
                            SELECT price_per_kwh FROM stations WHERE id = :station_id
                        """)
                        tariff_result = db.execute(tariff_query, {"station_id": self.id}).fetchone()
                        rate_per_kwh = float(tariff_result[0]) if tariff_result and tariff_result[0] else 6.5
                        
                        actual_cost = energy_consumed * rate_per_kwh
                        
                        # Получаем данные сессии для возврата средств
                        session_query = text("""
                            SELECT user_id, amount FROM charging_sessions 
                            WHERE id = :session_id
                        """)
                        session_result = db.execute(session_query, {"session_id": session_id}).fetchone()
                        
                        if session_result:
                            user_id = session_result[0]
                            reserved_amount = float(session_result[1]) if session_result[1] else 0
                            refund_amount = max(0, reserved_amount - actual_cost)
                            
                            # Обновляем сессию
                            update_session_query = text("""
                                UPDATE charging_sessions 
                                SET stop_time = NOW(), status = 'stopped', 
                                    energy = :energy_consumed, amount = :actual_cost
                                WHERE id = :session_id
                            """)
                            db.execute(update_session_query, {
                                "energy_consumed": energy_consumed,
                                "actual_cost": actual_cost,
                                "session_id": session_id
                            })
                            
                            # Возвращаем неиспользованные средства
                            if refund_amount > 0:
                                from app.crud.ocpp_service import payment_service
                                from decimal import Decimal
                                
                                current_balance_query = text("""
                                    SELECT balance FROM clients WHERE id = :client_id
                                """)
                                balance_result = db.execute(current_balance_query, {"client_id": user_id}).fetchone()
                                current_balance = Decimal(str(balance_result[0])) if balance_result else Decimal('0')
                                
                                new_balance = payment_service.update_client_balance(
                                    db, user_id, Decimal(str(refund_amount)), "add",
                                    f"Возврат неиспользованных средств за сессию {session_id}"
                                )
                                
                                payment_service.create_payment_transaction(
                                    db, user_id, "balance_topup",
                                    Decimal(str(refund_amount)),
                                    current_balance, new_balance,
                                    f"Возврат за сессию {session_id}: потреблено {energy_consumed:.3f} кВт⋅ч",
                                    charging_session_id=session_id
                                )
                            
                            self.logger.info(f"🏁 АВТОЗАВЕРШЕНИЕ: сессия {session_id}, {energy_consumed:.3f} кВт⋅ч, {actual_cost:.2f} сом, возврат {refund_amount:.2f} сом")
                    
                    except Exception as e:
                        self.logger.error(f"Ошибка автозавершения мобильной сессии {session_id}: {e}")
                
                # 🆕 АВТОМАТИЧЕСКОЕ ОБНОВЛЕНИЕ: Коннектор становится свободен
                if connector_id:
                    update_query = text("""
                        UPDATE connectors 
                        SET status = 'available', error_code = 'NoError', last_status_update = NOW()
                        WHERE station_id = :station_id AND connector_number = :connector_id
                    """)
                    db.execute(update_query, {"station_id": self.id, "connector_id": connector_id})
                
                db.commit()
                
                # Очищаем активные сессии
                if self.id in active_sessions:
                    del active_sessions[self.id]
                
            self.logger.info(f"Transaction completed: {transaction_id}, connector {connector_id} marked as Available")
            return call_result.StopTransaction(
                id_tag_info={"status": AuthorizationStatus.accepted}
            )
            
        except Exception as e:
            self.logger.error(f"Error in StopTransaction: {e}")
            return call_result.StopTransaction(
                id_tag_info={"status": AuthorizationStatus.invalid}
            )

    @on('MeterValues')
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        """Показания счетчиков энергии"""
        transaction_id = kwargs.get('transaction_id')
        self.logger.debug(f"MeterValues: connector={connector_id}, transaction_id={transaction_id}")
        
        try:
            with next(get_db()) as db:
                # 🔍 DEBUG: Логируем сырую структуру
                self.logger.info(f"🔍 RAW DEBUG: meter_value={meter_value}")
                self.logger.info(f"🔍 RAW DEBUG: type={type(meter_value)}")
                
                # Парсим timestamp
                timestamp_str = meter_value[0].get('timestamp') if meter_value else None
                if timestamp_str:
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', ''))
                else:
                    timestamp = datetime.utcnow()
                
                # Парсим sampled values
                sampled_values = []
                for mv in meter_value:
                    self.logger.info(f"🔍 MV DEBUG: mv={mv}")
                    sampled_value_list = mv.get('sampled_value', [])
                    self.logger.info(f"🔍 SV DEBUG: sampledValue={sampled_value_list}")
                    for sample in sampled_value_list:
                        self.logger.info(f"🔍 SAMPLE DEBUG: sample={sample}")
                        sampled_values.append({
                            'measurand': sample.get('measurand', ''),
                            'value': sample.get('value'),
                            'unit': sample.get('unit', ''),
                            'context': sample.get('context', ''),
                            'format': sample.get('format', ''),
                            'location': sample.get('location', '')
                        })
                
                self.logger.info(f"🔍 DEBUG: Parsed values: {sampled_values}")
                
                # Сохраняем показания
                OCPPMeterService.add_meter_values(
                    db, self.id, connector_id, timestamp, sampled_values, transaction_id
                )
                
                # 🔍 DEBUG: Проверяем активную сессию
                session = active_sessions.get(self.id)
                self.logger.info(f"🔍 DEBUG: Active session for {self.id}: {session}")
                
                if session and sampled_values:
                    for sample in sampled_values:
                        if sample['measurand'] == 'Energy.Active.Import.Register':
                            try:
                                current_energy = float(sample['value'])
                                meter_start = session.get('meter_start', 0.0)
                                energy_delivered = current_energy - meter_start
                                session['energy_delivered'] = energy_delivered
                                
                                self.logger.info(f"🔍 ENERGY DEBUG: current={current_energy}, start={meter_start}, delivered={energy_delivered}")
                                
                                # 🆕 ОБНОВЛЕНИЕ МОБИЛЬНОЙ СЕССИИ: Записываем энергию в charging_sessions
                                if session.get('client_id') and session.get('charging_session_id'):
                                    client_id = session['client_id']
                                    charging_session_id = session['charging_session_id']
                                    energy_kwh = energy_delivered / 1000.0  # Wh → kWh
                                    
                                    self.logger.info(f"🔄 ОБНОВЛЕНИЕ СЕССИИ: client_id={client_id}, session_id={charging_session_id}, energy={energy_kwh} кВтч")
                                    
                                    # Получаем тариф для расчета стоимости
                                    tariff_query = text("""
                                        SELECT price_per_kwh FROM stations 
                                        WHERE id = :station_id
                                    """)
                                    tariff_result = db.execute(tariff_query, {"station_id": self.id})
                                    tariff_row = tariff_result.fetchone()
                                    tariff_rate = float(tariff_row[0]) if tariff_row else 6.5
                                    
                                    # Рассчитываем текущую стоимость
                                    try:
                                        current_cost = Decimal(str(energy_kwh)) * Decimal(str(tariff_rate))
                                        
                                        # Обновляем мобильную сессию
                                        update_query = text("""
                                            UPDATE charging_sessions 
                                            SET energy = :energy, amount = :cost
                                            WHERE id = :session_id
                                        """)
                                        db.execute(update_query, {
                                            "energy": energy_kwh,
                                            "cost": float(current_cost),
                                            "session_id": charging_session_id
                                        })
                                        db.commit()
                                        
                                        self.logger.info(f"✅ СЕССИЯ ОБНОВЛЕНА: {energy_kwh} кВт⋅ч, {current_cost} сом")
                                        
                                    except Exception as cost_error:
                                        self.logger.error(f"🔍 COST ERROR: {cost_error}")
                                        # Обновляем только энергию без стоимости
                                        update_query = text("""
                                            UPDATE charging_sessions 
                                            SET energy = :energy
                                            WHERE id = :session_id
                                        """)
                                        db.execute(update_query, {
                                            "energy": energy_kwh,
                                            "session_id": charging_session_id
                                        })
                                        db.commit()
                                
                                # Проверка лимитов (если установлены)
                                energy_limit = session.get('energy_limit')
                                if energy_limit and energy_delivered >= energy_limit:
                                    self.logger.warning(f"Energy limit reached: {energy_delivered} >= {energy_limit}")
                                    # Отправляем команду через Redis для async обработки
                                    asyncio.create_task(
                                        redis_manager.publish_command(
                                            self.id, {"command": "RemoteStopTransaction"}
                                        )
                                    )
                                
                                active_sessions[self.id] = session
                                break
                            except (ValueError, TypeError) as e:
                                self.logger.error(f"🔍 VALUE ERROR: {e}")
                                continue
                else:
                    self.logger.warning(f"🔍 NO SESSION DEBUG: session={session}, sampled_values={bool(sampled_values)}")
                
        except Exception as e:
            self.logger.error(f"Error in MeterValues: {e}")
        
        return call_result.MeterValues()

    # ============================================================================
    # TIER 1: ДОПОЛНИТЕЛЬНЫЕ (критически важные)
    # ============================================================================

    @on('GetConfiguration')
    def on_get_configuration(self, **kwargs):
        """Получение конфигурационных параметров"""
        keys = kwargs.get('key', [])
        self.logger.info(f"GetConfiguration request for keys: {keys}")
        
        try:
            with next(get_db()) as db:
                configurations = OCPPConfigurationService.get_configuration(
                    db, self.id, keys
                )
                
                # Формируем ответ
                configuration_key = []
                unknown_key = []
                
                if keys:
                    # Проверяем запрошенные ключи
                    found_keys = {config.key for config in configurations}
                    for key in keys:
                        if key in found_keys:
                            config = next(c for c in configurations if c.key == key)
                            configuration_key.append({
                                "key": config.key,
                                "readonly": config.readonly,
                                "value": config.value
                            })
                        else:
                            unknown_key.append(key)
                else:
                    # Возвращаем все конфигурации
                    for config in configurations:
                        configuration_key.append({
                            "key": config.key,
                            "readonly": config.readonly,
                            "value": config.value
                        })
                
            return call_result.GetConfiguration(
                configuration_key=configuration_key,
                unknown_key=unknown_key if unknown_key else None
            )
            
        except Exception as e:
            self.logger.error(f"Error in GetConfiguration: {e}")
            return call_result.GetConfiguration(
                configuration_key=[],
                unknown_key=keys if keys else []
            )

    @on('ChangeConfiguration')
    def on_change_configuration(self, key, value, **kwargs):
        """Изменение конфигурационных параметров"""
        self.logger.info(f"ChangeConfiguration: {key} = {value}")
        
        try:
            with next(get_db()) as db:
                result = OCPPConfigurationService.change_configuration(
                    db, self.id, key, value
                )
                
            status = result.get("status", "Rejected")
            self.logger.info(f"ChangeConfiguration result: {status}")
            
            return call_result.ChangeConfiguration(
                status=ConfigurationStatus[status.lower()]
            )
            
        except Exception as e:
            self.logger.error(f"Error in ChangeConfiguration: {e}")
            return call_result.ChangeConfiguration(
                status=ConfigurationStatus.rejected
            )

    @on('Reset')
    def on_reset(self, type, **kwargs):
        """Перезапуск станции"""
        self.logger.info(f"Reset request: {type}")
        
        try:
            # Сохраняем информацию о reset в БД
            with next(get_db()) as db:
                OCPPStationService.update_station_status(
                    db, self.id, "Unavailable", "Reset", f"Reset {type} requested"
                )
            
            # В реальной реализации здесь может быть логика
            # для инициации перезапуска станции
            
            return call_result.Reset(status=ResetStatus.accepted)
            
        except Exception as e:
            self.logger.error(f"Error in Reset: {e}")
            return call_result.Reset(status=ResetStatus.rejected)

    @on('UnlockConnector')
    def on_unlock_connector(self, connector_id, **kwargs):
        """Разблокировка коннектора"""
        self.logger.info(f"UnlockConnector request for connector: {connector_id}")
        
        try:
            # В реальной реализации здесь проверяется
            # возможность разблокировки коннектора
            
            with next(get_db()) as db:
                # Проверяем наличие активной транзакции
                active_transaction = db.query(OCPPTransaction).filter(
                    OCPPTransaction.station_id == self.id,
                    OCPPTransaction.status == "started"
                ).first()
                
                if active_transaction:
                    # Есть активная транзакция - не можем разблокировать
                    return call_result.UnlockConnector(
                        status=UnlockStatus.not_supported
                    )
            
            return call_result.UnlockConnector(status=UnlockStatus.unlocked)
            
        except Exception as e:
            self.logger.error(f"Error in UnlockConnector: {e}")
            return call_result.UnlockConnector(status=UnlockStatus.unlock_failed)

    # ============================================================================
    # TIER 2: ВАЖНЫЕ ДЛЯ PRODUCTION
    # ============================================================================

    @on('DataTransfer')
    def on_data_transfer(self, vendor_id, **kwargs):
        """Кастомные данные производителя"""
        message_id = kwargs.get('message_id', '')
        data = kwargs.get('data', '')
        
        self.logger.info(f"DataTransfer: vendor_id={vendor_id}, message_id={message_id}")
        
        try:
            # Здесь можно добавить обработку кастомных данных
            # В зависимости от vendor_id и message_id
            
            return call_result.DataTransfer(
                status="Accepted",
                data=f"Received: {data}"
            )
            
        except Exception as e:
            self.logger.error(f"Error in DataTransfer: {e}")
            return call_result.DataTransfer(status="Rejected")

    @on('DiagnosticsStatusNotification')
    def on_diagnostics_status_notification(self, status, **kwargs):
        """Статус диагностики"""
        self.logger.info(f"DiagnosticsStatusNotification: status={status}")
        
        try:
            with next(get_db()) as db:
                # Сохраняем статус диагностики в конфигурации
                OCPPConfigurationService.set_configuration(
                    db, self.id, "DiagnosticsStatus", status
                )
                
            return call_result.DiagnosticsStatusNotification()
            
        except Exception as e:
            self.logger.error(f"Error in DiagnosticsStatusNotification: {e}")
            return call_result.DiagnosticsStatusNotification()

    @on('FirmwareStatusNotification')
    def on_firmware_status_notification(self, status, **kwargs):
        """Статус обновления прошивки"""
        self.logger.info(f"FirmwareStatusNotification: status={status}")
        
        try:
            with next(get_db()) as db:
                # Обновляем статус прошивки
                station_status = OCPPStationService.get_station_status(db, self.id)
                if station_status:
                    if status == FirmwareStatus.installed:
                        # Прошивка обновлена успешно
                        station_status.firmware_version = "Updated"
                    elif status == FirmwareStatus.installation_failed:
                        # Ошибка обновления
                        station_status.error_code = "FirmwareUpdateFailed"
                    
                    db.commit()
                
            return call_result.FirmwareStatusNotification()
            
        except Exception as e:
            self.logger.error(f"Error in FirmwareStatusNotification: {e}")
            return call_result.FirmwareStatusNotification()

    # ============================================================================
    # TIER 2: РАСШИРЕННЫЕ (важные для production)
    # ============================================================================

    @on('ChangeAvailability')
    def on_change_availability(self, connector_id, type, **kwargs):
        """Изменение доступности станции/коннектора"""
        self.logger.info(f"ChangeAvailability: connector={connector_id}, type={type}")
        
        try:
            with next(get_db()) as db:
                # Обновляем статус в зависимости от типа
                if type == AvailabilityType.operative:
                    new_status = "Available"
                else:  # inoperative
                    new_status = "Unavailable"
                
                OCPPStationService.update_station_status(
                    db, self.id, new_status, None, f"Availability changed to {type}"
                )
            
            return call_result.ChangeAvailability(
                status=AvailabilityStatus.accepted
            )
            
        except Exception as e:
            self.logger.error(f"Error in ChangeAvailability: {e}")
            return call_result.ChangeAvailability(
                status=AvailabilityStatus.rejected
            )

    @on('ClearCache')
    def on_clear_cache(self, **kwargs):
        """Очистка локального кэша авторизации"""
        self.logger.info("ClearCache request")
        
        try:
            with next(get_db()) as db:
                # Помечаем все локальные авторизации как устаревшие
                expired_auths = db.query(OCPPAuthorization).filter(
                    OCPPAuthorization.station_id == self.id,
                    OCPPAuthorization.is_local == True
                ).all()
                
                for auth in expired_auths:
                    auth.expires_at = datetime.utcnow()  # Помечаем как истекшие
                
                db.commit()
                
            self.logger.info(f"Cleared {len(expired_auths)} local authorizations")
            return call_result.ClearCache(status=ClearCacheStatus.accepted)
            
        except Exception as e:
            self.logger.error(f"Error in ClearCache: {e}")
            return call_result.ClearCache(status=ClearCacheStatus.rejected)

    @on('GetDiagnostics')
    def on_get_diagnostics(self, location, **kwargs):
        """Получение диагностических данных"""
        retries = kwargs.get('retries', 1)
        retry_interval = kwargs.get('retry_interval', 60)
        start_time = kwargs.get('start_time')
        stop_time = kwargs.get('stop_time')
        
        self.logger.info(f"GetDiagnostics request to location: {location}")
        
        try:
            # В реальной реализации здесь запускается процесс
            # сбора и отправки диагностических данных
            
            # Генерируем имя файла диагностики
            filename = f"diagnostics_{self.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log"
            
            # Сохраняем информацию о запросе диагностики
            with next(get_db()) as db:
                OCPPConfigurationService.set_configuration(
                    db, self.id, "DiagnosticsLocation", location
                )
                OCPPConfigurationService.set_configuration(
                    db, self.id, "DiagnosticsFilename", filename
                )
            
            return call_result.GetDiagnostics(file_name=filename)
            
        except Exception as e:
            self.logger.error(f"Error in GetDiagnostics: {e}")
            return call_result.GetDiagnostics(file_name=None)

    @on('UpdateFirmware')
    def on_update_firmware(self, location, retrieve_date, **kwargs):
        """Обновление прошивки"""
        retries = kwargs.get('retries', 1)
        retry_interval = kwargs.get('retry_interval', 60)
        
        self.logger.info(f"UpdateFirmware request: {location} at {retrieve_date}")
        
        try:
            # В реальной реализации здесь планируется загрузка
            # и установка новой прошивки
            
            with next(get_db()) as db:
                # Сохраняем информацию об обновлении
                OCPPConfigurationService.set_configuration(
                    db, self.id, "FirmwareUpdateLocation", location
                )
                OCPPConfigurationService.set_configuration(
                    db, self.id, "FirmwareUpdateDate", retrieve_date
                )
                
                # Обновляем статус станции
                OCPPStationService.update_station_status(
                    db, self.id, "Unavailable", None, "Firmware update scheduled"
                )
            
            return call_result.UpdateFirmware()
            
        except Exception as e:
            self.logger.error(f"Error in UpdateFirmware: {e}")
            return call_result.UpdateFirmware()

    # ============================================================================ 
    # TIER 3: ДОПОЛНИТЕЛЬНЫЕ (полезные для мониторинга)
    # ============================================================================

    @on('TriggerMessage')
    def on_trigger_message(self, requested_message, **kwargs):
        """Запрос отправки определенного сообщения"""
        connector_id = kwargs.get('connector_id')
        
        self.logger.info(f"TriggerMessage: {requested_message} for connector {connector_id}")
        
        try:
            # В зависимости от requested_message отправляем соответствующее сообщение
            if requested_message == MessageTrigger.boot_notification:
                # Планируем отправку BootNotification
                pass
            elif requested_message == MessageTrigger.heartbeat:
                # Планируем отправку Heartbeat
                pass
            elif requested_message == MessageTrigger.status_notification:
                # Планируем отправку StatusNotification
                pass
            elif requested_message == MessageTrigger.meter_values:
                # Планируем отправку MeterValues
                pass
            
            return call_result.TriggerMessage(
                status=TriggerMessageStatus.accepted
            )
            
        except Exception as e:
            self.logger.error(f"Error in TriggerMessage: {e}")
            return call_result.TriggerMessage(
                status=TriggerMessageStatus.rejected
            )

    @on('SendLocalList')
    def on_send_local_list(self, list_version, update_type, **kwargs):
        """Обновление локального списка авторизации"""
        local_authorization_list = kwargs.get('local_authorization_list', [])
        
        self.logger.info(f"SendLocalList: version={list_version}, type={update_type}")
        
        try:
            with next(get_db()) as db:
                if update_type == UpdateType.full:
                    # Полная замена списка
                    # Удаляем все локальные авторизации
                    db.query(OCPPAuthorization).filter(
                        OCPPAuthorization.station_id == self.id,
                        OCPPAuthorization.is_local == True
                    ).delete()
                
                # Добавляем новые записи
                for item in local_authorization_list:
                    id_tag = item.get('id_tag')
                    id_tag_info = item.get('id_tag_info', {})
                    
                    auth = OCPPAuthorization(
                        station_id=self.id,
                        id_tag=id_tag,
                        status=id_tag_info.get('status', 'Accepted'),
                        is_local=True,
                        expires_at=datetime.fromisoformat(id_tag_info['expiry_date']) 
                                   if id_tag_info.get('expiry_date') else None
                    )
                    db.add(auth)
                
                # Сохраняем версию списка
                OCPPConfigurationService.set_configuration(
                    db, self.id, "LocalListVersion", str(list_version)
                )
                
                db.commit()
            
            return call_result.SendLocalList(status=UpdateStatus.accepted)
            
        except Exception as e:
            self.logger.error(f"Error in SendLocalList: {e}")
            return call_result.SendLocalList(status=UpdateStatus.failed)

    @on('GetLocalListVersion')
    def on_get_local_list_version(self, **kwargs):
        """Получение версии локального списка авторизации"""
        self.logger.info("GetLocalListVersion request")
        
        try:
            with next(get_db()) as db:
                configs = OCPPConfigurationService.get_configuration(
                    db, self.id, ["LocalListVersion"]
                )
                
                version = 0
                if configs:
                    try:
                        version = int(configs[0].value)
                    except (ValueError, IndexError):
                        version = 0
            
            return call_result.GetLocalListVersion(list_version=version)
            
        except Exception as e:
            self.logger.error(f"Error in GetLocalListVersion: {e}")
            return call_result.GetLocalListVersion(list_version=0)


class OCPPWebSocketHandler:
    """Основной класс для обработки OCPP WebSocket подключений"""
    
    def __init__(self, station_id: str, websocket: WebSocket):
        self.station_id = station_id
        self.websocket = websocket
        self.charge_point: Optional[OCPPChargePoint] = None
        self.pubsub_task: Optional[asyncio.Task] = None
        self.logger = logging.getLogger(f"OCPPHandler.{station_id}")
        
    async def handle_connection(self):
        """Основная логика обработки WebSocket подключения"""
        try:
            # Принимаем WebSocket подключение с OCPP 1.6 subprotocol
            await self.websocket.accept(subprotocol="ocpp1.6")
            self.logger.info(f"Station {self.station_id} connected")
            
            # Создаем адаптер для OCPP библиотеки
            adapter = WebSocketAdapter(self.websocket)
            self.charge_point = OCPPChargePoint(self.station_id, adapter)
            
            # Регистрируем станцию в Redis
            await redis_manager.register_station(self.station_id)
            
            # Запускаем обработчик команд из Redis
            self.pubsub_task = asyncio.create_task(
                self._handle_redis_commands()
            )
            
            # Запускаем OCPP charge point
            await self.charge_point.start()
            
        except WebSocketDisconnect:
            self.logger.info(f"Station {self.station_id} disconnected")
        except Exception as e:
            self.logger.error(f"Error in WebSocket connection: {e}")
        finally:
            await self._cleanup()
    
    async def _handle_redis_commands(self):
        """Обработка команд из Redis pub/sub"""
        try:
            async for command in redis_manager.listen_commands(self.station_id):
                self.logger.info(f"Received command: {command}")
                
                if not self.charge_point:
                    continue
                    
                command_type = command.get("action")
                
                try:
                    if command_type == "RemoteStartTransaction":
                        response = await self.charge_point.call(
                            call.RemoteStartTransaction(
                                connector_id=command.get("connector_id", 1),
                                id_tag=command.get("id_tag", "system")
                            )
                        )
                        self.logger.info(f"RemoteStartTransaction response: {response}")
                        
                    elif command_type == "RemoteStopTransaction":
                        session = active_sessions.get(self.station_id, {})
                        transaction_id = session.get('transaction_id', 
                                                   command.get("transaction_id", 1))
                        
                        response = await self.charge_point.call(
                            call.RemoteStopTransaction(transaction_id=transaction_id)
                        )
                        self.logger.info(f"RemoteStopTransaction response: {response}")
                        
                    elif command_type == "Reset":
                        reset_type = command.get("type", "Soft")
                        response = await self.charge_point.call(
                            call.Reset(type=ResetType[reset_type.lower()])
                        )
                        self.logger.info(f"Reset response: {response}")
                        
                    elif command_type == "UnlockConnector":
                        connector_id = command.get("connectorId", 1)
                        response = await self.charge_point.call(
                            call.UnlockConnector(connector_id=connector_id)
                        )
                        self.logger.info(f"UnlockConnector response: {response}")
                        
                    elif command_type == "ChangeConfiguration":
                        key = command.get("key")
                        value = command.get("value")
                        if key and value:
                            response = await self.charge_point.call(
                                call.ChangeConfiguration(key=key, value=value)
                            )
                            self.logger.info(f"ChangeConfiguration response: {response}")
                            
                    elif command_type == "GetConfiguration":
                        keys = command.get("keys", [])
                        response = await self.charge_point.call(
                            call.GetConfiguration(key=keys if keys else None)
                        )
                        self.logger.info(f"GetConfiguration response: {response}")
                        
                    elif command_type == "ChangeAvailability":
                        connector_id = command.get("connectorId", 0)
                        availability_type = command.get("type", "Operative")
                        response = await self.charge_point.call(
                            call.ChangeAvailability(
                                connector_id=connector_id,
                                type=AvailabilityType[availability_type.lower()]
                            )
                        )
                        self.logger.info(f"ChangeAvailability response: {response}")
                        
                    elif command_type == "ClearCache":
                        response = await self.charge_point.call(call.ClearCache())
                        self.logger.info(f"ClearCache response: {response}")
                        
                    elif command_type == "GetDiagnostics":
                        location = command.get("location", "/tmp/diagnostics.log")
                        response = await self.charge_point.call(
                            call.GetDiagnostics(location=location)
                        )
                        self.logger.info(f"GetDiagnostics response: {response}")
                        
                    elif command_type == "UpdateFirmware":
                        location = command.get("location")
                        retrieve_date = command.get("retrieveDate")
                        if location and retrieve_date:
                            response = await self.charge_point.call(
                                call.UpdateFirmware(
                                    location=location,
                                    retrieve_date=retrieve_date
                                )
                            )
                            self.logger.info(f"UpdateFirmware response: {response}")
                            
                    elif command_type == "TriggerMessage":
                        requested_message = command.get("requestedMessage")
                        connector_id = command.get("connectorId")
                        if requested_message:
                            response = await self.charge_point.call(
                                call.TriggerMessage(
                                    requested_message=MessageTrigger[requested_message.lower()],
                                    connector_id=connector_id
                                )
                            )
                            self.logger.info(f"TriggerMessage response: {response}")
                            
                except Exception as e:
                    self.logger.error(f"Error executing command {command_type}: {e}")
                    
        except Exception as e:
            self.logger.error(f"Error in Redis command handler: {e}")
    
    async def _cleanup(self):
        """Очистка ресурсов при отключении"""
        try:
            if self.pubsub_task:
                self.pubsub_task.cancel()
                
            await redis_manager.unregister_station(self.station_id)
            
            if self.station_id in active_sessions:
                del active_sessions[self.station_id]
                
            self.logger.info(f"Cleanup completed for station {self.station_id}")
            
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")


class WebSocketAdapter:
    """Адаптер FastAPI WebSocket для совместимости с OCPP библиотекой"""
    
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
    
    async def recv(self):
        """Получение сообщения"""
        return await self.websocket.receive_text()
    
    async def send(self, message):
        """Отправка сообщения"""
        await self.websocket.send_text(message)
    
    async def close(self):
        """Закрытие соединения"""
        await self.websocket.close() 