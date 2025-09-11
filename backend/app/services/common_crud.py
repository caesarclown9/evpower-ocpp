"""
Общие CRUD операции для устранения дублирования SQL запросов
"""
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)

class CommonCrudService:
    """Общий сервис для часто используемых SQL операций"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_client_balance(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Получить баланс клиента"""
        result = self.db.execute(
            text("SELECT id, balance FROM clients WHERE id = :client_id"),
            {"client_id": client_id}
        )
        client = result.fetchone()
        if client:
            return {"id": client[0], "balance": float(client[1])}
        return None
    
    def update_client_balance(self, client_id: str, new_balance: float) -> bool:
        """Обновить баланс клиента"""
        try:
            self.db.execute(
                text("UPDATE clients SET balance = :balance WHERE id = :client_id"),
                {"balance": new_balance, "client_id": client_id}
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления баланса клиента {client_id}: {e}")
            return False
    
    def get_station_basic_info(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Получить базовую информацию о станции"""
        result = self.db.execute(
            text("""
                SELECT s.id, s.serial_number, s.model, s.manufacturer, s.status,
                       s.power_capacity, s.connector_types, s.connectors_count,
                       s.price_per_kwh, s.session_fee, s.currency
                FROM stations s WHERE s.id = :station_id
            """),
            {"station_id": station_id}
        )
        station = result.fetchone()
        if station:
            return {
                "id": station[0],
                "serial_number": station[1], 
                "model": station[2],
                "manufacturer": station[3],
                "status": station[4],
                "power_capacity": station[5],
                "connector_types": station[6],
                "connectors_count": station[7],
                "price_per_kwh": float(station[8]) if station[8] else 13.5,
                "session_fee": float(station[9]) if station[9] else 0.0,
                "currency": station[10] or "KGS"
            }
        return None
    
    def get_tariff_price(self, station_id: str) -> float:
        """Получить цену за кВт/ч для станции"""
        result = self.db.execute(
            text("""
                SELECT price FROM tariff_rules 
                WHERE tariff_plan_id = (
                    SELECT tariff_plan_id FROM stations WHERE id = :station_id
                ) AND is_active = true
                ORDER BY priority DESC LIMIT 1
            """),
            {"station_id": station_id}
        )
        tariff = result.fetchone()
        return float(tariff[0]) if tariff else 13.5
    
    def get_connector_status(self, station_id: str, connector_id: int) -> Optional[str]:
        """Получить статус коннектора"""
        result = self.db.execute(
            text("""
                SELECT status FROM connectors 
                WHERE station_id = :station_id AND connector_number = :connector_id
            """),
            {"station_id": station_id, "connector_id": connector_id}
        )
        connector = result.fetchone()
        return connector[0] if connector else None
    
    def update_connector_status(self, station_id: str, connector_id: int, status: str) -> bool:
        """Обновить статус коннектора"""
        try:
            self.db.execute(
                text("""
                    UPDATE connectors 
                    SET status = :status 
                    WHERE station_id = :station_id AND connector_number = :connector_id
                """),
                {"status": status, "station_id": station_id, "connector_id": connector_id}
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления статуса коннектора {station_id}:{connector_id}: {e}")
            return False
    
    def get_active_charging_session(self, client_id: str = None, station_id: str = None) -> Optional[Dict[str, Any]]:
        """Получить активную сессию зарядки"""
        conditions = ["cs.status = 'started'"]
        params = {}
        
        if client_id:
            conditions.append("cs.user_id = :client_id")
            params["client_id"] = client_id
            
        if station_id:
            conditions.append("cs.station_id = :station_id")
            params["station_id"] = station_id
        
        result = self.db.execute(
            text(f"""
                SELECT cs.id, cs.user_id, cs.station_id, cs.start_time, cs.status,
                       cs.energy, cs.amount, cs.limit_type, cs.limit_value
                FROM charging_sessions cs
                WHERE {' AND '.join(conditions)}
                ORDER BY cs.start_time DESC LIMIT 1
            """),
            params
        )
        
        session = result.fetchone()
        if session:
            return {
                "id": session[0],
                "user_id": session[1],
                "station_id": session[2], 
                "start_time": session[3],
                "status": session[4],
                "energy": float(session[5]) if session[5] else 0.0,
                "amount": float(session[6]) if session[6] else 0.0,
                "limit_type": session[7],
                "limit_value": float(session[8]) if session[8] else 0.0
            }
        return None
    
    def create_payment_transaction(self, client_id: str, transaction_type: str, 
                                 amount: float, balance_before: float, balance_after: float,
                                 description: str = None) -> bool:
        """Создать запись о транзакции"""
        try:
            self.db.execute(
                text("""
                    INSERT INTO payment_transactions_odengi 
                    (client_id, transaction_type, amount, balance_before, balance_after, description)
                    VALUES (:client_id, :transaction_type, :amount, :balance_before, :balance_after, :description)
                """),
                {
                    "client_id": client_id,
                    "transaction_type": transaction_type,
                    "amount": amount,
                    "balance_before": balance_before,
                    "balance_after": balance_after,
                    "description": description
                }
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка создания транзакции для {client_id}: {e}")
            return False