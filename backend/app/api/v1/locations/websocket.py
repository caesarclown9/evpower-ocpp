"""
WebSocket эндпоинт для получения realtime обновлений локаций
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from sqlalchemy.orm import Session
import json
import asyncio
import logging
from typing import Optional, Set

from app.db.session import get_db
from ocpp_ws_server.redis_manager import redis_manager

logger = logging.getLogger(__name__)
router = APIRouter()

# Хранилище активных WebSocket соединений
active_connections: Set[WebSocket] = set()


class LocationWebSocketManager:
    """
    Менеджер для управления WebSocket подключениями клиентов
    """
    
    def __init__(self):
        self.active_connections: dict = {}  # client_id -> WebSocket
        self.subscriptions: dict = {}  # client_id -> set of channels
    
    async def connect(self, websocket: WebSocket, client_id: str):
        """Подключение нового клиента"""
        await websocket.accept()
        self.active_connections[client_id] = websocket
        self.subscriptions[client_id] = set()
        logger.info(f"WebSocket клиент {client_id} подключен")
    
    def disconnect(self, client_id: str):
        """Отключение клиента"""
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            del self.subscriptions[client_id]
            logger.info(f"WebSocket клиент {client_id} отключен")
    
    async def subscribe(self, client_id: str, channel: str):
        """Подписка клиента на канал"""
        if client_id in self.subscriptions:
            self.subscriptions[client_id].add(channel)
            logger.debug(f"Клиент {client_id} подписан на канал {channel}")
    
    async def unsubscribe(self, client_id: str, channel: str):
        """Отписка клиента от канала"""
        if client_id in self.subscriptions:
            self.subscriptions[client_id].discard(channel)
            logger.debug(f"Клиент {client_id} отписан от канала {channel}")
    
    async def send_personal_message(self, message: str, client_id: str):
        """Отправка сообщения конкретному клиенту"""
        if client_id in self.active_connections:
            websocket = self.active_connections[client_id]
            try:
                await websocket.send_text(message)
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения клиенту {client_id}: {e}")
                self.disconnect(client_id)
    
    async def broadcast(self, message: str, channel: str):
        """Рассылка сообщения всем подписчикам канала"""
        for client_id, channels in self.subscriptions.items():
            if channel in channels:
                await self.send_personal_message(message, client_id)


# Глобальный менеджер WebSocket соединений
ws_manager = LocationWebSocketManager()


@router.websocket("/ws/locations")
async def websocket_locations(
    websocket: WebSocket,
    client_id: Optional[str] = Query(None)
):
    """
    WebSocket для получения обновлений статусов локаций в реальном времени
    
    Клиент может подписаться на обновления:
    - Всех локаций: {"action": "subscribe", "channel": "all"}
    - Конкретной локации: {"action": "subscribe", "channel": "location:location_id"}
    - Станций локации: {"action": "subscribe", "channel": "location_stations:location_id"}
    
    Формат входящих сообщений:
    {
        "action": "subscribe" | "unsubscribe" | "ping",
        "channel": "all" | "location:id" | "location_stations:id"
    }
    
    Формат исходящих сообщений:
    {
        "type": "location_status_update" | "station_status_update" | "pong",
        "data": {...}
    }
    """
    # Генерируем ID клиента если не передан
    if not client_id:
        import uuid
        client_id = str(uuid.uuid4())
    
    await ws_manager.connect(websocket, client_id)
    
    # Создаем задачу для прослушивания Redis
    redis_listener_task = None
    
    try:
        # Автоматически подписываем на все локации
        await ws_manager.subscribe(client_id, "location_updates:all")
        
        # Запускаем прослушивание Redis
        redis_listener_task = asyncio.create_task(
            listen_redis_updates(client_id)
        )
        
        # Отправляем приветственное сообщение
        await websocket.send_json({
            "type": "connection",
            "status": "connected",
            "client_id": client_id,
            "message": "Подключено к обновлениям локаций"
        })
        
        # Основной цикл обработки сообщений от клиента
        while True:
            try:
                # Ждем сообщение от клиента
                data = await websocket.receive_text()
                message = json.loads(data)
                
                action = message.get("action")
                channel_name = message.get("channel")
                
                if action == "subscribe" and channel_name:
                    # Подписка на канал
                    if channel_name == "all":
                        redis_channel = "location_updates:all"
                    elif channel_name.startswith("location:"):
                        location_id = channel_name.split(":", 1)[1]
                        redis_channel = f"location_updates:{location_id}"
                    elif channel_name.startswith("location_stations:"):
                        location_id = channel_name.split(":", 1)[1]
                        redis_channel = f"location_stations:{location_id}"
                    else:
                        redis_channel = channel_name
                    
                    await ws_manager.subscribe(client_id, redis_channel)
                    
                    await websocket.send_json({
                        "type": "subscription",
                        "status": "subscribed",
                        "channel": channel_name
                    })
                    
                elif action == "unsubscribe" and channel_name:
                    # Отписка от канала
                    if channel_name == "all":
                        redis_channel = "location_updates:all"
                    else:
                        redis_channel = channel_name
                    
                    await ws_manager.unsubscribe(client_id, redis_channel)
                    
                    await websocket.send_json({
                        "type": "subscription",
                        "status": "unsubscribed",
                        "channel": channel_name
                    })
                    
                elif action == "ping":
                    # Пинг-понг для проверки соединения
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": asyncio.get_event_loop().time()
                    })
                    
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "message": "Неверный формат JSON"
                })
            except Exception as e:
                logger.error(f"Ошибка обработки сообщения от {client_id}: {e}")
                
    except WebSocketDisconnect:
        logger.info(f"WebSocket клиент {client_id} отключился")
    except Exception as e:
        logger.error(f"Ошибка WebSocket для клиента {client_id}: {e}")
    finally:
        # Отменяем задачу прослушивания Redis
        if redis_listener_task:
            redis_listener_task.cancel()
        
        # Отключаем клиента
        ws_manager.disconnect(client_id)


async def listen_redis_updates(client_id: str):
    """
    Прослушивание обновлений из Redis и отправка клиенту
    """
    try:
        # Получаем подписки клиента
        while client_id in ws_manager.subscriptions:
            channels = ws_manager.subscriptions.get(client_id, set())
            
            for channel in channels:
                # Проверяем наличие сообщений в канале
                message = await redis_manager.get_message(channel)
                
                if message:
                    # Отправляем сообщение клиенту
                    await ws_manager.send_personal_message(message, client_id)
            
            # Небольшая задержка между проверками
            await asyncio.sleep(0.1)
            
    except asyncio.CancelledError:
        logger.debug(f"Прослушивание Redis для клиента {client_id} отменено")
    except Exception as e:
        logger.error(f"Ошибка прослушивания Redis для клиента {client_id}: {e}")