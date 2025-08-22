"""
Сервис для агрегации и управления статусами локаций
"""
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from datetime import datetime, timedelta
import logging
import json

from ocpp_ws_server.redis_manager import redis_manager

logger = logging.getLogger(__name__)


class LocationStatusService:
    """
    Сервис для определения и кэширования статусов локаций
    """
    
    # Время жизни кэша в секундах
    CACHE_TTL = 30
    
    @staticmethod
    def determine_location_status(
        available_stations: int,
        occupied_stations: int,
        offline_stations: int,
        maintenance_stations: int
    ) -> str:
        """
        Определяет статус локации на основе статусов станций
        
        Приоритет статусов:
        1. offline - если есть офлайн станции
        2. maintenance - если есть станции на обслуживании
        3. occupied - если ВСЕ станции заняты
        4. available - если ВСЕ станции свободны
        5. partial - если есть и свободные и занятые
        """
        total_stations = available_stations + occupied_stations + offline_stations + maintenance_stations
        
        if total_stations == 0:
            return "offline"
        
        # Приоритет 1: Офлайн
        if offline_stations > 0:
            return "offline"
        
        # Приоритет 2: Обслуживание
        if maintenance_stations > 0:
            return "maintenance"
        
        # Приоритет 3: Все заняты
        if occupied_stations == total_stations:
            return "occupied"
        
        # Приоритет 4: Все свободны
        if available_stations == total_stations:
            return "available"
        
        # Приоритет 5: Частично доступны
        if available_stations > 0 and occupied_stations > 0:
            return "partial"
        
        # По умолчанию
        return "offline"
    
    @classmethod
    async def get_locations_with_status(cls, db: Session) -> List[Dict[str, Any]]:
        """
        Получает все локации с агрегированными статусами
        """
        # Проверяем кэш
        cache_key = "locations_status:all"
        cached_data = await redis_manager.get_cached_data(cache_key)
        
        if cached_data:
            logger.debug("Возвращаем статусы локаций из кэша")
            return json.loads(cached_data)
        
        # Запрос для получения локаций и их статусов
        query = text("""
            WITH station_statuses AS (
                SELECT 
                    s.location_id,
                    s.id as station_id,
                    s.status as admin_status,
                    s.is_available,
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
                        WHEN EXISTS (
                            SELECT 1 FROM connectors c 
                            WHERE c.station_id = s.id 
                            AND c.status = 'occupied'
                        ) THEN 'occupied'
                        ELSE 'offline'
                    END as calculated_status
                FROM stations s
                WHERE s.status != 'inactive'
            ),
            connector_counts AS (
                SELECT 
                    s.location_id,
                    COUNT(DISTINCT c.id) as total_connectors,
                    COUNT(DISTINCT CASE WHEN c.status = 'available' THEN c.id END) as available_connectors,
                    COUNT(DISTINCT CASE WHEN c.status = 'occupied' THEN c.id END) as occupied_connectors,
                    COUNT(DISTINCT CASE WHEN c.status = 'faulted' THEN c.id END) as faulted_connectors
                FROM stations s
                LEFT JOIN connectors c ON s.id = c.station_id
                WHERE s.status != 'inactive'
                GROUP BY s.location_id
            )
            SELECT 
                l.id,
                l.name,
                l.address,
                l.city,
                l.country,
                l.latitude,
                l.longitude,
                l.stations_count,
                l.connectors_count,
                l.status as admin_status,
                COUNT(DISTINCT ss.station_id) as total_stations,
                COUNT(DISTINCT CASE WHEN ss.calculated_status = 'available' THEN ss.station_id END) as available_stations,
                COUNT(DISTINCT CASE WHEN ss.calculated_status = 'occupied' THEN ss.station_id END) as occupied_stations,
                COUNT(DISTINCT CASE WHEN ss.calculated_status = 'offline' THEN ss.station_id END) as offline_stations,
                COUNT(DISTINCT CASE WHEN ss.calculated_status = 'maintenance' THEN ss.station_id END) as maintenance_stations,
                COALESCE(cc.total_connectors, 0) as total_connectors,
                COALESCE(cc.available_connectors, 0) as available_connectors,
                COALESCE(cc.occupied_connectors, 0) as occupied_connectors,
                COALESCE(cc.faulted_connectors, 0) as faulted_connectors
            FROM locations l
            LEFT JOIN station_statuses ss ON l.id = ss.location_id
            LEFT JOIN connector_counts cc ON l.id = cc.location_id
            WHERE l.status = 'active'
            GROUP BY 
                l.id, l.name, l.address, l.city, l.country, 
                l.latitude, l.longitude, l.stations_count, 
                l.connectors_count, l.status,
                cc.total_connectors, cc.available_connectors,
                cc.occupied_connectors, cc.faulted_connectors
            ORDER BY l.name
        """)
        
        result = db.execute(query)
        locations = []
        
        for row in result:
            # Определяем статус локации
            location_status = cls.determine_location_status(
                available_stations=row[11],
                occupied_stations=row[12],
                offline_stations=row[13],
                maintenance_stations=row[14]
            )
            
            location_data = {
                "id": row[0],
                "name": row[1],
                "address": row[2],
                "city": row[3],
                "country": row[4],
                "coordinates": {
                    "latitude": row[5],
                    "longitude": row[6]
                },
                "status": location_status,
                "stations_summary": {
                    "total": row[10],
                    "available": row[11],
                    "occupied": row[12],
                    "offline": row[13],
                    "maintenance": row[14]
                },
                "connectors_summary": {
                    "total": row[15],
                    "available": row[16],
                    "occupied": row[17],
                    "faulted": row[18]
                }
            }
            
            locations.append(location_data)
        
        # Кэшируем результат
        await redis_manager.cache_data(cache_key, json.dumps(locations), cls.CACHE_TTL)
        
        logger.info(f"Получены статусы для {len(locations)} локаций")
        return locations
    
    @classmethod
    async def get_location_details(cls, db: Session, location_id: str) -> Optional[Dict[str, Any]]:
        """
        Получает детальную информацию о локации включая все станции
        """
        # Проверяем кэш
        cache_key = f"location_status:{location_id}"
        cached_data = await redis_manager.get_cached_data(cache_key)
        
        if cached_data:
            logger.debug(f"Возвращаем статус локации {location_id} из кэша")
            return json.loads(cached_data)
        
        # Получаем информацию о локации
        location_query = text("""
            SELECT 
                id, name, address, city, country, 
                latitude, longitude, status,
                stations_count, connectors_count
            FROM locations
            WHERE id = :location_id AND status = 'active'
        """)
        
        location_result = db.execute(location_query, {"location_id": location_id}).fetchone()
        
        if not location_result:
            return None
        
        # Получаем станции локации с их статусами
        stations_query = text("""
            SELECT 
                s.id,
                s.serial_number,
                s.model,
                s.manufacturer,
                s.status as admin_status,
                s.power_capacity,
                s.connectors_count,
                s.price_per_kwh,
                s.session_fee,
                s.currency,
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
                    WHEN EXISTS (
                        SELECT 1 FROM connectors c 
                        WHERE c.station_id = s.id 
                        AND c.status = 'occupied'
                    ) THEN 'occupied'
                    ELSE 'offline'
                END as calculated_status,
                (
                    SELECT COUNT(*) FROM connectors c 
                    WHERE c.station_id = s.id AND c.status = 'available'
                ) as available_connectors,
                (
                    SELECT COUNT(*) FROM connectors c 
                    WHERE c.station_id = s.id AND c.status = 'occupied'
                ) as occupied_connectors,
                (
                    SELECT COUNT(*) FROM connectors c 
                    WHERE c.station_id = s.id AND c.status = 'faulted'
                ) as faulted_connectors
            FROM stations s
            WHERE s.location_id = :location_id AND s.status != 'inactive'
            ORDER BY s.serial_number
        """)
        
        stations_result = db.execute(stations_query, {"location_id": location_id})
        
        stations = []
        available_count = 0
        occupied_count = 0
        offline_count = 0
        maintenance_count = 0
        
        total_connectors = 0
        available_connectors = 0
        occupied_connectors = 0
        faulted_connectors = 0
        
        for station in stations_result:
            station_status = station[11]
            
            # Подсчитываем статусы станций
            if station_status == 'available':
                available_count += 1
            elif station_status == 'occupied':
                occupied_count += 1
            elif station_status == 'offline':
                offline_count += 1
            elif station_status == 'maintenance':
                maintenance_count += 1
            
            # Подсчитываем коннекторы
            available_connectors += station[12]
            occupied_connectors += station[13]
            faulted_connectors += station[14]
            total_connectors += station[6]
            
            station_data = {
                "id": station[0],
                "serial_number": station[1],
                "model": station[2],
                "manufacturer": station[3],
                "status": station_status,
                "power_capacity": float(station[5]) if station[5] else 0,
                "connectors_count": station[6],
                "tariff": {
                    "price_per_kwh": float(station[7]) if station[7] else 0,
                    "session_fee": float(station[8]) if station[8] else 0,
                    "currency": station[9] or "KGS"
                },
                "connectors_summary": {
                    "available": station[12],
                    "occupied": station[13],
                    "faulted": station[14]
                }
            }
            
            stations.append(station_data)
        
        # Определяем статус локации
        location_status = cls.determine_location_status(
            available_stations=available_count,
            occupied_stations=occupied_count,
            offline_stations=offline_count,
            maintenance_stations=maintenance_count
        )
        
        location_details = {
            "id": location_result[0],
            "name": location_result[1],
            "address": location_result[2],
            "city": location_result[3],
            "country": location_result[4],
            "coordinates": {
                "latitude": location_result[5],
                "longitude": location_result[6]
            },
            "status": location_status,
            "stations_summary": {
                "total": len(stations),
                "available": available_count,
                "occupied": occupied_count,
                "offline": offline_count,
                "maintenance": maintenance_count
            },
            "connectors_summary": {
                "total": total_connectors,
                "available": available_connectors,
                "occupied": occupied_connectors,
                "faulted": faulted_connectors
            },
            "stations": stations
        }
        
        # Кэшируем результат
        await redis_manager.cache_data(cache_key, json.dumps(location_details), cls.CACHE_TTL)
        
        return location_details
    
    @classmethod
    async def invalidate_cache(cls, location_id: Optional[str] = None):
        """
        Инвалидирует кэш для локации или всех локаций
        """
        if location_id:
            cache_key = f"location_status:{location_id}"
            await redis_manager.delete(cache_key)
            logger.info(f"Кэш локации {location_id} инвалидирован")
        
        # Всегда инвалидируем общий кэш
        await redis_manager.delete("locations_status:all")
        logger.info("Общий кэш локаций инвалидирован")