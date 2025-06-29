# redis_manager.py
# Асинхронный менеджер для работы с Redis: хранение подключённых станций и Pub/Sub для команд
import redis.asyncio as redis
import json
import os
import logging

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# 🔍 DEBUG: выводим реальный Redis URL
logger.info(f"🔍 REDIS_MANAGER DEBUG: REDIS_URL = {REDIS_URL}")

class RedisOcppManager:
    def __init__(self):
        # 🔍 DEBUG: выводим URL при инициализации
        logger.info(f"🔍 REDIS_MANAGER INIT: Using REDIS_URL = {REDIS_URL}")
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)

    async def ping(self) -> bool:
        """Проверка соединения с Redis"""
        try:
            result = await self.redis.ping()
            logger.info(f"✅ REDIS PING SUCCESS: {result}")
            return True
        except Exception as e:
            logger.error(f"❌ REDIS PING FAILED: {e}")
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

redis_manager = RedisOcppManager() 