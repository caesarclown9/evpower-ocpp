import signal
import asyncio
import logging
from typing import Set, Callable
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

class GracefulShutdownManager:
    """Менеджер для graceful shutdown приложения"""
    
    def __init__(self):
        self.is_shutting_down = False
        self.active_connections: Set = set()
        self.shutdown_callbacks: list[Callable] = []
        self.shutdown_timeout = 30  # секунд
    
    def add_connection(self, connection):
        """Добавляет активное соединение для отслеживания"""
        if not self.is_shutting_down:
            self.active_connections.add(connection)
    
    def remove_connection(self, connection):
        """Удаляет соединение из активных"""
        self.active_connections.discard(connection)
    
    def add_shutdown_callback(self, callback: Callable):
        """Добавляет callback для выполнения при shutdown"""
        self.shutdown_callbacks.append(callback)
    
    async def initiate_shutdown(self):
        """Инициирует graceful shutdown"""
        if self.is_shutting_down:
            return
        
        logger.info("🛑 Initiating graceful shutdown...")
        self.is_shutting_down = True
        
        # Выполняем shutdown callbacks
        for callback in self.shutdown_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
            except Exception as e:
                logger.error(f"Error during shutdown callback: {e}")
        
        # Ждем завершения активных соединений
        logger.info(f"⏳ Waiting for {len(self.active_connections)} active connections to close...")
        
        timeout = self.shutdown_timeout
        start_time = asyncio.get_event_loop().time()
        
        while self.active_connections and timeout > 0:
            await asyncio.sleep(0.1)
            elapsed = asyncio.get_event_loop().time() - start_time
            timeout = self.shutdown_timeout - elapsed
        
        if self.active_connections:
            logger.warning(f"⚠️ Force closing {len(self.active_connections)} remaining connections")
            for connection in list(self.active_connections):
                try:
                    if hasattr(connection, 'close'):
                        await connection.close()
                    elif hasattr(connection, 'disconnect'):
                        await connection.disconnect()
                except Exception as e:
                    logger.error(f"Error closing connection: {e}")
        
        logger.info("✅ Graceful shutdown completed")

# Глобальный экземпляр
shutdown_manager = GracefulShutdownManager()

def setup_signal_handlers():
    """Настраивает обработчики сигналов для graceful shutdown"""
    
    def signal_handler(signum, frame):
        logger.info(f"📡 Received signal {signum}")
        
        # Создаем задачу для graceful shutdown
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(shutdown_manager.initiate_shutdown())
        else:
            asyncio.run(shutdown_manager.initiate_shutdown())
    
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    logger.info("📡 Signal handlers registered for graceful shutdown")

@asynccontextmanager
async def connection_manager(connection):
    """Context manager для автоматического управления соединениями"""
    shutdown_manager.add_connection(connection)
    try:
        yield connection
    finally:
        shutdown_manager.remove_connection(connection) 