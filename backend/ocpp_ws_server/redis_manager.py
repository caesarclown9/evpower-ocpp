# redis_manager.py
# Асинхронный менеджер для работы с Redis: хранение подключённых станций и Pub/Sub для команд
import redis.asyncio as redis_async
import redis as redis_sync  # Синхронный клиент для OCPP handlers
import json
import os
import logging
import asyncio
from typing import Optional, Set, Dict, AsyncGenerator

logger = logging.getLogger(__name__)

# Константы
STATION_TTL_SECONDS = 300  # 5 минут TTL для онлайн-статуса станции
SUBSCRIPTION_TIMEOUT_SECONDS = 5.0  # Таймаут ожидания подписки


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

        # Создаем асинхронное соединение
        self.redis = redis_async.from_url(redis_url, decode_responses=True)

        # Создаем синхронное соединение для OCPP handlers (которые синхронные)
        self.redis_sync = redis_sync.from_url(redis_url, decode_responses=True)
        logger.info("Redis manager: Sync client initialized for OCPP handlers")

        # Словарь для отслеживания готовности подписок станций
        self._subscription_ready: Dict[str, asyncio.Event] = {}

        # Активные pubsub подписки (для корректного закрытия)
        self._active_pubsubs: Dict[str, redis_async.client.PubSub] = {}

    async def ping(self) -> bool:
        """Проверка соединения с Redis"""
        try:
            result = await self.redis.ping()
            logger.debug(f"Redis ping: {result}")
            return True
        except Exception as e:
            logger.error(f"Redis ping failed: {e}")
            return False

    # ============================================================
    # СТАНЦИИ: TTL-based регистрация (вместо SET)
    # ============================================================

    async def register_station(self, station_id: str):
        """
        Регистрация станции с TTL.
        Ключ автоматически истечёт через 5 минут если не будет продлён.
        """
        key = f"ocpp:station:{station_id}"
        await self.redis.setex(key, STATION_TTL_SECONDS, "online")
        logger.info(f"✅ Station {station_id} registered (TTL: {STATION_TTL_SECONDS}s)")

    async def refresh_station_ttl(self, station_id: str):
        """
        Продление TTL станции (вызывается при каждом Heartbeat).
        """
        key = f"ocpp:station:{station_id}"
        # Проверяем существование и продлеваем
        exists = await self.redis.exists(key)
        if exists:
            await self.redis.expire(key, STATION_TTL_SECONDS)
            logger.debug(f"🔄 Station {station_id} TTL refreshed")
        else:
            # Станция не была зарегистрирована - регистрируем
            await self.register_station(station_id)

    async def unregister_station(self, station_id: str):
        """
        Явное удаление станции (при disconnect).
        """
        key = f"ocpp:station:{station_id}"
        await self.redis.delete(key)

        # Очищаем подписку
        if station_id in self._subscription_ready:
            del self._subscription_ready[station_id]

        # Закрываем pubsub если есть
        if station_id in self._active_pubsubs:
            try:
                pubsub = self._active_pubsubs.pop(station_id)
                await pubsub.unsubscribe()
                await pubsub.close()
            except Exception as e:
                logger.warning(f"Error closing pubsub for {station_id}: {e}")

        logger.info(f"🔌 Station {station_id} unregistered")

    async def is_station_online(self, station_id: str) -> bool:
        """
        Проверка онлайн-статуса станции.
        """
        key = f"ocpp:station:{station_id}"
        return await self.redis.exists(key) == 1

    async def get_stations(self) -> Set[str]:
        """
        Получение списка всех онлайн станций.
        Сканирует ключи ocpp:station:* (работает с TTL подходом).
        """
        stations = set()
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(cursor, match="ocpp:station:*", count=100)
            for key in keys:
                # Извлекаем station_id из ключа "ocpp:station:{station_id}"
                station_id = key.replace("ocpp:station:", "")
                stations.add(station_id)
            if cursor == 0:
                break
        return stations

    # ============================================================
    # SUBSCRIPTION READY: механизм ожидания готовности подписки
    # ============================================================

    async def wait_for_subscription(self, station_id: str, timeout: float = SUBSCRIPTION_TIMEOUT_SECONDS) -> bool:
        """
        Ожидание готовности подписки станции на команды.

        Args:
            station_id: ID станции
            timeout: Таймаут ожидания в секундах

        Returns:
            True если подписка готова, False если таймаут
        """
        event = self._subscription_ready.get(station_id)
        if event is None:
            logger.warning(f"⚠️ No subscription event found for {station_id}")
            return False

        if event.is_set():
            return True

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            logger.debug(f"✅ Subscription ready for {station_id}")
            return True
        except asyncio.TimeoutError:
            logger.warning(f"⏰ Subscription timeout for {station_id} ({timeout}s)")
            return False

    async def is_subscription_ready(self, station_id: str) -> bool:
        """
        Проверка готовности подписки (без ожидания).
        """
        event = self._subscription_ready.get(station_id)
        return event is not None and event.is_set()

    def _mark_subscription_ready(self, station_id: str):
        """
        Отметить подписку как готовую (вызывается из listen_commands).
        """
        if station_id not in self._subscription_ready:
            self._subscription_ready[station_id] = asyncio.Event()
        self._subscription_ready[station_id].set()
        logger.info(f"✅ Subscription ready event set for {station_id}")

    # ============================================================
    # PUB/SUB: команды для станций
    # ============================================================

    async def publish_command(self, station_id: str, command: dict):
        """
        Публикация команды для станции.
        """
        channel = f"ocpp:cmd:{station_id}"
        message = json.dumps(command)
        await self.redis.publish(channel, message)
        logger.info(f"📤 Command published to {station_id}: {command.get('action', 'unknown')}")

    async def listen_commands(self, station_id: str) -> AsyncGenerator[dict, None]:
        """
        Подписка на команды для станции.
        Отмечает подписку как готовую после успешной подписки.
        """
        channel = f"ocpp:cmd:{station_id}"

        # Создаём Event для отслеживания готовности
        if station_id not in self._subscription_ready:
            self._subscription_ready[station_id] = asyncio.Event()

        pubsub = self.redis.pubsub()
        self._active_pubsubs[station_id] = pubsub

        try:
            await pubsub.subscribe(channel)
            logger.info(f"📡 Subscribed to commands channel: {channel}")

            # Отмечаем подписку как готовую
            self._mark_subscription_ready(station_id)

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        command = json.loads(message["data"])
                        logger.debug(f"📥 Received command for {station_id}: {command.get('action', 'unknown')}")
                        yield command
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON in command: {e}")
        except asyncio.CancelledError:
            logger.info(f"🛑 Command listener cancelled for {station_id}")
            raise
        except Exception as e:
            logger.error(f"Error in command listener for {station_id}: {e}")
            raise
        finally:
            # Очистка при завершении
            if station_id in self._active_pubsubs:
                del self._active_pubsubs[station_id]
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception as e:
                logger.warning(f"Error cleaning up pubsub for {station_id}: {e}")

    # ============================================================
    # ТРАНЗАКЦИИ: кэширование OCPP транзакций
    # ============================================================

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

    # ============================================================
    # КЭШИРОВАНИЕ: общие методы
    # ============================================================

    async def cache_data(self, key: str, value: str, ttl: int = 30):
        """Кэширование данных с TTL"""
        await self.redis.setex(key, ttl, value)

    async def get_cached_data(self, key: str) -> Optional[str]:
        """Получение данных из кэша"""
        return await self.redis.get(key)

    async def delete(self, key: str):
        """Удаление ключа из кэша"""
        await self.redis.delete(key)

    # ============================================================
    # СИНХРОННЫЕ МЕТОДЫ: для использования в OCPP handlers
    # ============================================================

    def get_sync(self, key: str) -> Optional[str]:
        """Синхронное получение данных (для OCPP handlers)"""
        try:
            return self.redis_sync.get(key)
        except Exception as e:
            logger.error(f"Redis sync get error for {key}: {e}")
            return None

    def set_sync(self, key: str, value: str, ttl: int = None):
        """Синхронная запись данных (для OCPP handlers)"""
        try:
            if ttl:
                self.redis_sync.setex(key, ttl, value)
            else:
                self.redis_sync.set(key, value)
        except Exception as e:
            logger.error(f"Redis sync set error for {key}: {e}")

    def delete_sync(self, key: str):
        """Синхронное удаление ключа (для OCPP handlers)"""
        try:
            self.redis_sync.delete(key)
        except Exception as e:
            logger.error(f"Redis sync delete error for {key}: {e}")

    # ============================================================
    # ДИАГНОСТИКА
    # ============================================================

    async def get_diagnostics(self) -> dict:
        """
        Диагностическая информация о состоянии Redis.
        """
        try:
            stations = await self.get_stations()
            ready_subscriptions = [
                sid for sid, event in self._subscription_ready.items()
                if event.is_set()
            ]

            return {
                "redis_connected": await self.ping(),
                "online_stations": list(stations),
                "online_stations_count": len(stations),
                "ready_subscriptions": ready_subscriptions,
                "ready_subscriptions_count": len(ready_subscriptions),
                "active_pubsubs": list(self._active_pubsubs.keys())
            }
        except Exception as e:
            logger.error(f"Error getting diagnostics: {e}")
            return {"error": str(e)}

    # ============================================================
    # PUB/SUB: общие методы для realtime обновлений
    # ============================================================

    async def publish(self, channel: str, message: str):
        """Публикация сообщения в канал"""
        result = await self.redis.publish(channel, message)
        logger.info(f"📢 Published to {channel}, subscribers: {result}")

    async def subscribe_and_listen(self, *channels) -> AsyncGenerator[dict, None]:
        """
        Подписка и прослушивание нескольких каналов через Pub/Sub.
        Используется для WebSocket клиентов (location updates).

        Args:
            *channels: Названия каналов для подписки

        Yields:
            dict: Сообщения с полями 'channel' и 'data'
        """
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe(*channels)
            logger.info(f"📡 Subscribed to channels: {channels}")

            async for message in pubsub.listen():
                logger.debug(f"📨 RAW MESSAGE: {message}")
                if message["type"] == "message":
                    logger.info(f"📩 Pub/Sub message received on {message['channel']}")
                    yield {
                        "channel": message["channel"],
                        "data": message["data"]
                    }
        except asyncio.CancelledError:
            logger.info(f"🛑 Pub/Sub listener cancelled for channels: {channels}")
            raise
        finally:
            try:
                await pubsub.unsubscribe(*channels)
                await pubsub.close()
            except Exception as e:
                logger.warning(f"Error cleaning up pubsub: {e}")


# Глобальный экземпляр менеджера
redis_manager = RedisOcppManager()
