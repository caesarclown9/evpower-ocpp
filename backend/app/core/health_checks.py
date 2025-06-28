import asyncio
import time
import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta
import psutil
import httpx

from ocpp_ws_server.redis_manager import redis_manager

logger = logging.getLogger(__name__)

class HealthChecker:
    """Класс для комплексной проверки здоровья системы"""
    
    def __init__(self):
        self.checks = {}
        self.last_check_time = None
        self.check_interval = 30  # секунд
    
    async def comprehensive_health_check(self) -> Dict[str, Any]:
        """Выполняет комплексную проверку здоровья системы"""
        now = time.time()
        
        # Кэшируем результаты на 30 секунд
        if (self.last_check_time and 
            now - self.last_check_time < self.check_interval and 
            self.checks):
            return self.checks
        
        checks = {}
        
        # 1. Redis connectivity
        checks["redis"] = await self._check_redis()
        
        # 2. System resources
        checks["system"] = await self._check_system_resources()
        
        # 3. External services
        checks["external_services"] = await self._check_external_services()
        
        # 4. OCPP connections
        checks["ocpp"] = await self._check_ocpp_connections()
        
        # Общий статус
        overall_status = "healthy"
        critical_issues = []
        
        for check_name, check_result in checks.items():
            if not check_result.get("status") == "healthy":
                if check_result.get("critical", False):
                    overall_status = "unhealthy"
                    critical_issues.append(f"{check_name}: {check_result.get('message', 'Unknown error')}")
                elif overall_status == "healthy":
                    overall_status = "degraded"
        
        result = {
            "status": overall_status,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "checks": checks,
            "critical_issues": critical_issues
        }
        
        self.checks = result
        self.last_check_time = now
        
        return result
    
    async def _check_redis(self) -> Dict[str, Any]:
        """Проверка Redis подключения и производительности"""
        try:
            start_time = time.time()
            
            # Проверяем ping
            ping_result = await redis_manager.ping()
            if not ping_result:
                return {
                    "status": "unhealthy",
                    "critical": True,
                    "message": "Redis ping failed",
                    "response_time": None
                }
            
            ping_time = time.time() - start_time
            
            # Проверяем операции чтения/записи
            start_time = time.time()
            test_key = f"health_check_{int(time.time())}"
            test_value = "health_check_value"
            
            await redis_manager.redis.set(test_key, test_value, ex=10)
            retrieved_value = await redis_manager.redis.get(test_key)
            await redis_manager.redis.delete(test_key)
            
            rw_time = time.time() - start_time
            
            if retrieved_value.decode() != test_value:
                return {
                    "status": "unhealthy",
                    "critical": True,
                    "message": "Redis read/write test failed",
                    "ping_time": round(ping_time * 1000, 2),
                    "rw_time": round(rw_time * 1000, 2)
                }
            
            # Получаем статистику Redis
            info = await redis_manager.redis.info()
            memory_usage = info.get('used_memory_human', 'unknown')
            connected_clients = info.get('connected_clients', 0)
            
            return {
                "status": "healthy",
                "critical": True,
                "message": "Redis operational",
                "ping_time_ms": round(ping_time * 1000, 2),
                "rw_time_ms": round(rw_time * 1000, 2),
                "memory_usage": memory_usage,
                "connected_clients": connected_clients
            }
            
        except Exception as e:
            logger.error(f"Redis health check failed: {e}")
            return {
                "status": "unhealthy",
                "critical": True,
                "message": f"Redis error: {str(e)}"
            }
    
    async def _check_system_resources(self) -> Dict[str, Any]:
        """Проверка системных ресурсов"""
        try:
            # CPU usage
            cpu_percent = psutil.cpu_percent(interval=1)
            
            # Memory usage
            memory = psutil.virtual_memory()
            memory_percent = memory.percent
            memory_available_gb = memory.available / (1024**3)
            
            # Disk usage
            disk = psutil.disk_usage('/')
            disk_percent = disk.percent
            disk_free_gb = disk.free / (1024**3)
            
            # Определяем статус
            status = "healthy"
            warnings = []
            
            if cpu_percent > 80:
                status = "degraded"
                warnings.append(f"High CPU usage: {cpu_percent}%")
            
            if memory_percent > 85:
                status = "degraded"
                warnings.append(f"High memory usage: {memory_percent}%")
            
            if disk_percent > 90:
                status = "degraded"
                warnings.append(f"High disk usage: {disk_percent}%")
            
            if memory_available_gb < 0.5:
                status = "unhealthy"
                warnings.append(f"Low memory available: {memory_available_gb:.1f}GB")
            
            return {
                "status": status,
                "critical": False,
                "message": "System resources checked",
                "cpu_percent": round(cpu_percent, 1),
                "memory_percent": round(memory_percent, 1),
                "memory_available_gb": round(memory_available_gb, 1),
                "disk_percent": round(disk_percent, 1),
                "disk_free_gb": round(disk_free_gb, 1),
                "warnings": warnings
            }
            
        except Exception as e:
            logger.error(f"System resource check failed: {e}")
            return {
                "status": "degraded",
                "critical": False,
                "message": f"System check error: {str(e)}"
            }
    
    async def _check_external_services(self) -> Dict[str, Any]:
        """Проверка внешних сервисов"""
        services = {}
        
        # Проверяем Supabase
        try:
            import os
            supabase_url = os.getenv('SUPABASE_URL')
            if supabase_url:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    start_time = time.time()
                    response = await client.get(f"{supabase_url}/rest/v1/", 
                                              headers={"apikey": os.getenv('SUPABASE_ANON_KEY', '')})
                    response_time = time.time() - start_time
                    
                    services["supabase"] = {
                        "status": "healthy" if response.status_code == 200 else "degraded",
                        "response_time_ms": round(response_time * 1000, 2),
                        "status_code": response.status_code
                    }
        except Exception as e:
            services["supabase"] = {
                "status": "degraded",
                "error": str(e)
            }
        
        # Проверяем O!Dengi (только ping, без real API call)
        try:
            odengi_url = "https://api.odengi.com"
            async with httpx.AsyncClient(timeout=5.0) as client:
                start_time = time.time()
                response = await client.get(f"{odengi_url}/ping", timeout=3.0)
                response_time = time.time() - start_time
                
                services["odengi"] = {
                    "status": "healthy" if response.status_code in [200, 404] else "degraded",
                    "response_time_ms": round(response_time * 1000, 2),
                    "status_code": response.status_code
                }
        except Exception as e:
            services["odengi"] = {
                "status": "degraded",
                "error": str(e)
            }
        
        # Общий статус внешних сервисов
        overall_status = "healthy"
        for service_status in services.values():
            if service_status.get("status") != "healthy":
                overall_status = "degraded"
                break
        
        return {
            "status": overall_status,
            "critical": False,
            "message": "External services checked",
            "services": services
        }
    
    async def _check_ocpp_connections(self) -> Dict[str, Any]:
        """Проверка OCPP подключений"""
        try:
            connected_stations = await redis_manager.get_stations()
            station_count = len(connected_stations)
            
            # Получаем статистику подключений
            connection_stats = {}
            for station_id in connected_stations:
                try:
                    # Получаем последнюю активность станции
                    last_seen = await redis_manager.redis.get(f"station:{station_id}:last_seen")
                    if last_seen:
                        last_seen_time = datetime.fromisoformat(last_seen.decode())
                        minutes_ago = (datetime.utcnow() - last_seen_time).total_seconds() / 60
                        connection_stats[station_id] = {
                            "last_seen_minutes_ago": round(minutes_ago, 1)
                        }
                except Exception:
                    connection_stats[station_id] = {"last_seen_minutes_ago": "unknown"}
            
            return {
                "status": "healthy",
                "critical": False,
                "message": f"{station_count} stations connected",
                "connected_stations": station_count,
                "station_details": connection_stats
            }
            
        except Exception as e:
            logger.error(f"OCPP connections check failed: {e}")
            return {
                "status": "degraded",
                "critical": False,
                "message": f"OCPP check error: {str(e)}"
            }

# Глобальный экземпляр health checker
health_checker = HealthChecker() 