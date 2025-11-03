# redis_manager.py
# Асинхронный менеджер для работы с Redis: хранение подключённых станций и Pub/Sub для команд
import redis.asyncio as redis
import json
import os
import logging

logger = logging.getLogger(__name__)

class RedisOcppManager:
    def __init__(self):
        # Получаем настройки из config
        try:
            from app.core.config import settings
            redis_url = settings.REDIS_URL
            redis_password = settings.REDIS_PASSWORD
        except ImportError:
            # Fallback если config недоступен
            redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
            redis_password = os.getenv("REDIS_PASSWORD", None)

        # Логируем конфигурацию без секретных данных
        logger.info(f"Redis manager: Initializing (password: {'Yes' if redis_password else 'No'})")
        
        # Создаем соединение без принудительной аутентификации
        self.redis = redis.from_url(redis_url, decode_responses=True)

    async def ping(self) -> bool:
        """Проверка соединения с Redis"""
        try:
            result = await self.redis.ping()
            logger.debug(f"Redis ping: {result}")
            return True
        except Exception as e:
            logger.error(f"Redis ping failed: {e}")
            return False

    async def register_station(self, station_id: str):
        await self.redis.sadd("ocpp:stations", station_id)

    async def unregister_station(self, station_id: str):
        await self.redis.srem("ocpp:stations", station_id)

    async def get_stations(self):
        return await self.redis.smembers("ocpp:stations")

    async def publish_command(self, station_id: str, command: dict):
        channel = f"ocpp:cmd:{station_id}"
        await self.redis.publish(channel, json.dumps(command))

    async def listen_commands(self, station_id: str):
        channel = f"ocpp:cmd:{station_id}"
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(channel)
        async for message in pubsub.listen():
            if message["type"] == "message":
                yield json.loads(message["data"])

    async def add_transaction(self, station_id: str, transaction: dict):
        key = f"ocpp:transactions:{station_id}"
        await self.redis.rpush(key, json.dumps(transaction))

    async def get_transactions(self, station_id: str = None):
        if station_id:
            key = f"ocpp:transactions:{station_id}"
            txs = await self.redis.lrange(key, 0, -1)
            return [json.loads(tx) for tx in txs]
        else:
            # Получить все ключи транзакций
            keys = await self.redis.keys("ocpp:transactions:*")
            all_txs = []
            for key in keys:
                txs = await self.redis.lrange(key, 0, -1)
                all_txs.extend([json.loads(tx) for tx in txs])
            return all_txs
    
    # Методы для кэширования данных
    async def cache_data(self, key: str, value: str, ttl: int = 30):
        """Кэширование данных с TTL"""
        await self.redis.setex(key, ttl, value)
    
    async def get_cached_data(self, key: str):
        """Получение данных из кэша"""
        return await self.redis.get(key)
    
    async def delete(self, key: str):
        """Удаление ключа из кэша"""
        await self.redis.delete(key)
    
    # Методы для Pub/Sub (для realtime обновлений)
    async def publish(self, channel: str, message: str):
        """Публикация сообщения в канал"""
        await self.redis.publish(channel, message)
    
    async def get_message(self, channel: str):
        """Получение сообщения из канала (неблокирующее)"""
        # Это упрощенная версия - в реальности нужен более сложный механизм
        # Но для текущей реализации достаточно
        return None

redis_manager = RedisOcppManager() 