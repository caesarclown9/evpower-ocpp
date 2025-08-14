#!/usr/bin/env python3
"""
Скрипт для автоматического обновления статуса станций на основе heartbeat
Запускать через cron каждую минуту или как системный сервис
"""
import os
import logging
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Настройки
HEARTBEAT_TIMEOUT_MINUTES = 5  # Станция считается offline если нет heartbeat 5 минут
DATABASE_URL = os.getenv("DATABASE_URL")

def update_station_statuses():
    """Обновляет статусы станций на основе последнего heartbeat"""
    
    if not DATABASE_URL:
        logger.error("DATABASE_URL не установлен в .env")
        return
    
    try:
        # Создаем подключение к БД
        engine = create_engine(DATABASE_URL)
        
        with engine.connect() as conn:
            # Начинаем транзакцию
            trans = conn.begin()
            
            try:
                # 1. Обновляем станции, которые не отправляли heartbeat более 5 минут - ставим inactive
                inactive_query = text("""
                    UPDATE stations s
                    SET status = 'inactive', updated_at = NOW()
                    FROM ocpp_station_status oss
                    WHERE s.id = oss.station_id
                      AND s.status = 'active'
                      AND (oss.last_heartbeat IS NULL OR oss.last_heartbeat < NOW() - INTERVAL :timeout)
                    RETURNING s.id, s.serial_number
                """)
                
                result_inactive = conn.execute(
                    inactive_query,
                    {"timeout": f"{HEARTBEAT_TIMEOUT_MINUTES} minutes"}
                )
                
                inactive_stations = result_inactive.fetchall()
                if inactive_stations:
                    logger.info(f"Станции переведены в inactive: {[s[1] for s in inactive_stations]}")
                
                # 2. Обновляем станции, которые отправляли heartbeat в последние 5 минут - ставим active
                active_query = text("""
                    UPDATE stations s
                    SET status = 'active', updated_at = NOW()
                    FROM ocpp_station_status oss
                    WHERE s.id = oss.station_id
                      AND s.status = 'inactive'
                      AND oss.last_heartbeat >= NOW() - INTERVAL :timeout
                    RETURNING s.id, s.serial_number
                """)
                
                result_active = conn.execute(
                    active_query,
                    {"timeout": f"{HEARTBEAT_TIMEOUT_MINUTES} minutes"}
                )
                
                active_stations = result_active.fetchall()
                if active_stations:
                    logger.info(f"Станции переведены в active: {[s[1] for s in active_stations]}")
                
                # 3. Станции без записи в ocpp_station_status считаем inactive
                no_heartbeat_query = text("""
                    UPDATE stations
                    SET status = 'inactive', updated_at = NOW()
                    WHERE status = 'active'
                      AND id NOT IN (
                        SELECT station_id 
                        FROM ocpp_station_status 
                        WHERE last_heartbeat >= NOW() - INTERVAL :timeout
                      )
                    RETURNING id, serial_number
                """)
                
                result_no_heartbeat = conn.execute(
                    no_heartbeat_query,
                    {"timeout": f"{HEARTBEAT_TIMEOUT_MINUTES} minutes"}
                )
                
                no_heartbeat_stations = result_no_heartbeat.fetchall()
                if no_heartbeat_stations:
                    logger.info(f"Станции без heartbeat переведены в inactive: {[s[1] for s in no_heartbeat_stations]}")
                
                # 4. Логируем статистику
                stats_query = text("""
                    SELECT 
                        COUNT(*) FILTER (WHERE status = 'active') as active_count,
                        COUNT(*) FILTER (WHERE status = 'inactive') as inactive_count,
                        COUNT(*) as total_count
                    FROM stations
                """)
                
                stats = conn.execute(stats_query).fetchone()
                logger.info(f"Статистика станций - Активные: {stats[0]}, Неактивные: {stats[1]}, Всего: {stats[2]}")
                
                # Коммитим транзакцию
                trans.commit()
                logger.info("Статусы станций успешно обновлены")
                
            except Exception as e:
                trans.rollback()
                logger.error(f"Ошибка при обновлении статусов: {e}")
                raise
                
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise

def main():
    """Основная функция"""
    logger.info("Запуск обновления статусов станций...")
    update_station_statuses()
    logger.info("Обновление завершено")

if __name__ == "__main__":
    main()