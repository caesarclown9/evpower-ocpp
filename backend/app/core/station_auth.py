"""
Аутентификация для зарядных станций через API ключи
"""
import os
import logging
from typing import Optional, Dict, Any
from fastapi import WebSocket, Query, HTTPException
from sqlalchemy import text
from datetime import datetime
import hmac
import hashlib

from app.db.session import get_db

logger = logging.getLogger(__name__)

class StationAuth:
    """Обработчик аутентификации станций"""
    
    def __init__(self):
        self.verify_api_keys = os.getenv("VERIFY_STATION_API_KEYS", "true").lower() == "true"
        self.master_api_key = os.getenv("STATION_MASTER_API_KEY", "")
        
        if not self.verify_api_keys:
            logger.warning("⚠️ Station API key verification is DISABLED! Enable in production!")
    
    async def verify_station_connection(
        self, 
        station_id: str,
        api_key: Optional[str] = None,
        websocket: Optional[WebSocket] = None
    ) -> bool:
        """
        Проверяет право станции на подключение
        
        Args:
            station_id: ID станции
            api_key: API ключ для аутентификации
            websocket: WebSocket объект для получения заголовков
            
        Returns:
            True если станция авторизована
        """
        if not self.verify_api_keys:
            logger.debug(f"Station {station_id} auth check disabled")
            return True
        
        # Получаем API ключ из разных источников
        if not api_key and websocket:
            # Пробуем получить из заголовков
            headers = dict(websocket.headers)
            api_key = headers.get("authorization", "").replace("Bearer ", "")
            
            if not api_key:
                # Пробуем из query параметров
                api_key = websocket.query_params.get("token", "")
        
        if not api_key:
            logger.warning(f"Station {station_id} connection attempt without API key")
            return False
        
        # Проверяем master ключ
        if self.master_api_key and api_key == self.master_api_key:
            logger.info(f"Station {station_id} authenticated with master key")
            return True
        
        # Проверяем в базе данных
        try:
            db = next(get_db())
            
            # Проверяем API ключ станции
            result = db.execute(text("""
                SELECT s.id, s.api_key, s.status, s.api_key_expires_at
                FROM stations s
                WHERE s.id = :station_id
                AND s.status = 'active'
            """), {"station_id": station_id})
            
            station = result.fetchone()
            
            if not station:
                logger.warning(f"Station {station_id} not found or inactive")
                return False
            
            # Проверяем API ключ
            stored_api_key = station[1]
            if not stored_api_key:
                logger.warning(f"Station {station_id} has no API key configured")
                return False
            
            # Используем hmac.compare_digest для защиты от timing attacks
            if not hmac.compare_digest(api_key, stored_api_key):
                logger.warning(f"Station {station_id} invalid API key")
                return False
            
            # Проверяем срок действия ключа
            expires_at = station[3]
            if expires_at and expires_at < datetime.utcnow():
                logger.warning(f"Station {station_id} API key expired")
                return False
            
            logger.info(f"Station {station_id} successfully authenticated")
            
            # Обновляем последнее время использования
            db.execute(text("""
                UPDATE stations 
                SET last_api_key_use = NOW()
                WHERE id = :station_id
            """), {"station_id": station_id})
            db.commit()
            
            return True
            
        except Exception as e:
            logger.error(f"Error verifying station {station_id}: {e}")
            return False
        finally:
            if 'db' in locals():
                db.close()
    
    def generate_api_key(self, station_id: str) -> str:
        """
        Генерирует новый API ключ для станции
        
        Args:
            station_id: ID станции
            
        Returns:
            Новый API ключ
        """
        # Генерируем безопасный ключ
        import secrets
        timestamp = str(datetime.utcnow().timestamp())
        random_part = secrets.token_hex(16)
        
        # Создаем подписанный ключ
        message = f"{station_id}:{timestamp}:{random_part}"
        signature = hmac.new(
            self.master_api_key.encode() if self.master_api_key else b"default",
            message.encode(),
            hashlib.sha256
        ).hexdigest()[:16]
        
        return f"evp_{station_id}_{random_part}_{signature}"
    
    async def log_station_connection(
        self,
        station_id: str,
        event_type: str,
        extra_data: Dict[str, Any] = None
    ):
        """Логирует события подключения станции"""
        log_data = {
            "event": f"station_{event_type}",
            "station_id": station_id,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        if extra_data:
            log_data.update(extra_data)
        
        logger.info(f"Station {event_type}: {station_id}", extra=log_data)

# Глобальный экземпляр
station_auth = StationAuth()

async def verify_station_api_key(
    station_id: str,
    api_key: Optional[str] = Query(None, alias="token"),
    websocket: Optional[WebSocket] = None
) -> bool:
    """Dependency для проверки API ключа станции"""
    return await station_auth.verify_station_connection(station_id, api_key, websocket)