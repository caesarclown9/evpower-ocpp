"""
Сервис для отправки Realtime обновлений через WebSocket
"""
from typing import Dict, Any, Optional
import json
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import text

from ocpp_ws_server.redis_manager import redis_manager

logger = logging.getLogger(__name__)


class RealtimeService:
    """
    Сервис для отправки обновлений статусов в реальном времени
    """
    
    @staticmethod
    async def broadcast_location_update(db: Session, location_id: str):
        """
        Отправляет обновление статуса локации всем подписчикам
        """
        try:
            # Получаем актуальный статус локации из материализованного представления
            query = text("""
                SELECT 
                    id,
                    name,
                    location_status,
                    total_stations,
                    available_stations,
                    occupied_stations,
                    offline_stations,
                    maintenance_stations,
                    total_connectors,
                    available_connectors,
                    occupied_connectors,
                    faulted_connectors
                FROM location_status_view
                WHERE id = :location_id
            """)
            
            result = db.execute(query, {"location_id": location_id}).fetchone()
            
            if result:
                update_data = {
                    "type": "location_status_update",
                    "location_id": result[0],
                    "location_name": result[1],
                    "status": result[2],
                    "stations_summary": {
                        "total": result[3],
                        "available": result[4],
                        "occupied": result[5],
                        "offline": result[6],
                        "maintenance": result[7]
                    },
                    "connectors_summary": {
                        "total": result[8],
                        "available": result[9],
                        "occupied": result[10],
                        "faulted": result[11]
                    },
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                # Публикуем через Redis для всех подписчиков
                channel = f"location_updates:{location_id}"
                await redis_manager.publish(channel, json.dumps(update_data))
                
                # Также публикуем в общий канал всех локаций
                await redis_manager.publish("location_updates:all", json.dumps(update_data))
                
                logger.info(f"Отправлено обновление статуса локации {location_id}")
                
        except Exception as e:
            logger.error(f"Ошибка отправки обновления локации {location_id}: {e}")
    
    @staticmethod
    async def broadcast_station_update(db: Session, station_id: str):
        """
        Отправляет обновление статуса станции всем подписчикам
        """
        try:
            # Получаем информацию о станции
            query = text("""
                SELECT 
                    s.id,
                    s.serial_number,
                    s.location_id,
                    s.status,
                    s.last_heartbeat_at,
                    CASE 
                        WHEN s.status = 'maintenance' THEN 'maintenance'
                        WHEN s.last_heartbeat_at IS NULL OR 
                             s.last_heartbeat_at < NOW() - INTERVAL '5 minutes' THEN 'offline'
                        WHEN EXISTS (
                            SELECT 1 FROM connectors c 
                            WHERE c.station_id = s.id 
                            AND c.status = 'available'
                        ) THEN 'available'
                        ELSE 'occupied'
                    END as calculated_status,
                    (
                        SELECT COUNT(*) FROM connectors c 
                        WHERE c.station_id = s.id AND c.status = 'available'
                    ) as available_connectors,
                    (
                        SELECT COUNT(*) FROM connectors c 
                        WHERE c.station_id = s.id AND c.status = 'occupied'
                    ) as occupied_connectors
                FROM stations s
                WHERE s.id = :station_id
            """)
            
            result = db.execute(query, {"station_id": station_id}).fetchone()
            
            if result:
                update_data = {
                    "type": "station_status_update",
                    "station_id": result[0],
                    "serial_number": result[1],
                    "location_id": result[2],
                    "status": result[5],
                    "available_connectors": result[6],
                    "occupied_connectors": result[7],
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                # Публикуем для станции
                channel = f"station_updates:{station_id}"
                await redis_manager.publish(channel, json.dumps(update_data))
                
                # Публикуем для локации
                if result[2]:  # location_id
                    location_channel = f"location_stations:{result[2]}"
                    await redis_manager.publish(location_channel, json.dumps(update_data))
                    
                    # Также обновляем статус самой локации
                    await RealtimeService.broadcast_location_update(db, result[2])
                
                logger.info(f"Отправлено обновление статуса станции {station_id}")
                
        except Exception as e:
            logger.error(f"Ошибка отправки обновления станции {station_id}: {e}")
    
    @staticmethod
    async def broadcast_connector_update(db: Session, station_id: str, connector_id: int):
        """
        Отправляет обновление статуса коннектора всем подписчикам
        """
        try:
            # Получаем информацию о коннекторе
            query = text("""
                SELECT 
                    c.id,
                    c.connector_number,
                    c.status,
                    c.error_code,
                    c.connector_type,
                    c.power_kw,
                    s.location_id
                FROM connectors c
                JOIN stations s ON c.station_id = s.id
                WHERE c.station_id = :station_id 
                AND c.connector_number = :connector_id
            """)
            
            result = db.execute(query, {
                "station_id": station_id, 
                "connector_id": connector_id
            }).fetchone()
            
            if result:
                update_data = {
                    "type": "connector_status_update",
                    "connector_id": result[1],
                    "station_id": station_id,
                    "location_id": result[6],
                    "status": result[2],
                    "error_code": result[3],
                    "connector_type": result[4],
                    "power_kw": float(result[5]) if result[5] else 0,
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                # Публикуем для коннектора
                channel = f"connector_updates:{station_id}:{connector_id}"
                await redis_manager.publish(channel, json.dumps(update_data))
                
                # Также обновляем статус станции
                await RealtimeService.broadcast_station_update(db, station_id)
                
                logger.info(f"Отправлено обновление статуса коннектора {station_id}:{connector_id}")
                
        except Exception as e:
            logger.error(f"Ошибка отправки обновления коннектора {station_id}:{connector_id}: {e}")
    
    @staticmethod
    async def broadcast_charging_session_update(db: Session, session_id: str, event_type: str):
        """
        Отправляет обновление сессии зарядки
        
        event_type: 'started', 'stopped', 'error', 'meter_update'
        """
        try:
            # Получаем информацию о сессии
            query = text("""
                SELECT 
                    cs.id,
                    cs.user_id,
                    cs.station_id,
                    cs.start_time,
                    cs.stop_time,
                    cs.energy,
                    cs.amount,
                    cs.status,
                    s.location_id,
                    s.serial_number
                FROM charging_sessions cs
                JOIN stations s ON cs.station_id = s.id
                WHERE cs.id = :session_id
            """)
            
            result = db.execute(query, {"session_id": session_id}).fetchone()
            
            if result:
                update_data = {
                    "type": "charging_session_update",
                    "event": event_type,
                    "session_id": result[0],
                    "client_id": result[1],
                    "station_id": result[2],
                    "location_id": result[8],
                    "status": result[7],
                    "energy_kwh": float(result[5]) if result[5] else 0,
                    "amount": float(result[6]) if result[6] else 0,
                    "start_time": result[3].isoformat() if result[3] else None,
                    "stop_time": result[4].isoformat() if result[4] else None,
                    "timestamp": datetime.utcnow().isoformat()
                }
                
                # Публикуем для клиента
                client_channel = f"client_sessions:{result[1]}"
                await redis_manager.publish(client_channel, json.dumps(update_data))
                
                # Публикуем для станции
                station_channel = f"station_sessions:{result[2]}"
                await redis_manager.publish(station_channel, json.dumps(update_data))
                
                logger.info(f"Отправлено обновление сессии {session_id} (событие: {event_type})")
                
        except Exception as e:
            logger.error(f"Ошибка отправки обновления сессии {session_id}: {e}")