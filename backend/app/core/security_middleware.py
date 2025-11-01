import time
import ipaddress
from typing import Dict, Optional
from fastapi import Request, Response, HTTPException
from fastapi.responses import JSONResponse
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

from .logging_config import set_correlation_id, log_security_event
from app.core.config import settings

logger = logging.getLogger(__name__)

class RateLimiter:
    """Rate limiter с использованием sliding window"""
    
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: Dict[str, deque] = defaultdict(deque)
    
    def is_allowed(self, identifier: str) -> bool:
        """Проверяет, разрешен ли запрос"""
        now = time.time()
        window_start = now - self.window_seconds
        
        # Очищаем старые запросы
        while self.requests[identifier] and self.requests[identifier][0] < window_start:
            self.requests[identifier].popleft()
        
        # Проверяем лимит
        if len(self.requests[identifier]) >= self.max_requests:
            return False
        
        # Добавляем текущий запрос
        self.requests[identifier].append(now)
        return True

class SecurityMiddleware:
    """Middleware для безопасности"""
    
    def __init__(self):
        # Основной лимит и окно из ENV
        default_rpm = int(settings.__dict__.get("RATE_LIMIT_DEFAULT_PER_MINUTE", 60))
        self.rate_limiter = RateLimiter(max_requests=default_rpm, window_seconds=60)
        # Критичный лимит для денежных/старт/стоп операций
        self.critical_rpm = int(settings.__dict__.get("RATE_LIMIT_CRITICAL_PER_MINUTE", 10))
        self.critical_rate_limiter = RateLimiter(max_requests=self.critical_rpm, window_seconds=60)
        # Webhook лимит для защиты от DDoS на платежные уведомления
        self.webhook_rpm = int(settings.__dict__.get("RATE_LIMIT_WEBHOOK_PER_MINUTE", 30))
        self.webhook_rate_limiter = RateLimiter(max_requests=self.webhook_rpm, window_seconds=60)
        self.suspicious_ips = set()
        self.blocked_ips = set()
        
        # Список подозрительных User-Agent
        self.suspicious_user_agents = [
            'sqlmap', 'nikto', 'dirb', 'dirbuster', 'nmap', 
            'masscan', 'zap', 'burp', 'nessus', 'openvas'
        ]
    
    async def __call__(self, request: Request, call_next):
        # Устанавливаем correlation ID
        correlation_id = set_correlation_id()
        
        # Получаем IP клиента
        client_ip = self._get_client_ip(request)
        
        # Проверяем заблокированные IP
        if client_ip in self.blocked_ips:
            log_security_event("blocked_ip_attempt", source_ip=client_ip)
            return JSONResponse(
                status_code=403,
                content={"error": "Access denied"}
            )
        
        # Rate limiting (персональный идентификатор - client_id если есть)
        client_id = getattr(request.state, "client_id", None)
        identifier = client_id or client_ip
        # Определяем критичный маршрут
        path_lower = str(request.url.path).lower()

        # Проверяем webhook endpoints (специальный лимит для защиты от DDoS)
        is_webhook = request.method.upper() == "POST" and "/payment/webhook" in path_lower

        if is_webhook:
            # Для webhook используем IP-based rate limiting (без client_id)
            # так как это внешние запросы от платежных провайдеров
            if not self.webhook_rate_limiter.is_allowed(client_ip):
                log_security_event("rate_limit_exceeded_webhook", source_ip=client_ip)
                logger.warning(f"Webhook rate limit exceeded from {client_ip}")
                return JSONResponse(
                    status_code=429,
                    content={"success": False, "error": "too_many_requests", "message": "Webhook rate limit exceeded"}
                )

        # Определяем другие критичные маршруты
        is_critical = request.method.upper() == "POST" and (
            path_lower.endswith("/charging/start")
            or path_lower.endswith("/charging/stop")
            or path_lower.endswith("/balance/topup-qr")
            or path_lower.endswith("/balance/topup-card")
        )

        if is_critical:
            if not self.critical_rate_limiter.is_allowed(identifier):
                log_security_event("rate_limit_exceeded_critical", source_ip=client_ip)
                return JSONResponse(
                    status_code=429,
                    content={"success": False, "error": "too_many_requests", "message": "Rate limit exceeded (critical)"}
                )

        if not self.rate_limiter.is_allowed(identifier):
            log_security_event("rate_limit_exceeded", source_ip=client_ip)
            return JSONResponse(
                status_code=429,
                content={"success": False, "error": "too_many_requests", "message": "Rate limit exceeded"}
            )
        
        # Проверяем на подозрительные запросы
        if self._is_suspicious_request(request):
            self.suspicious_ips.add(client_ip)
            log_security_event("suspicious_request", 
                             source_ip=client_ip,
                             user_agent=request.headers.get("user-agent"),
                             path=str(request.url.path))
        
        # Добавляем security headers
        start_time = time.time()
        
        try:
            response = await call_next(request)
            
            # Логируем запрос
            process_time = time.time() - start_time
            logger.info(f"Request processed",
                       extra={
                           "method": request.method,
                           "path": str(request.url.path),
                           "client_ip": client_ip,
                           "status_code": response.status_code,
                           "process_time": round(process_time, 3),
                           "correlation_id": correlation_id
                       })
            
            # Добавляем security headers
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["X-Correlation-ID"] = correlation_id
            response.headers["Permissions-Policy"] = "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
            # Используем CSP настройки из env переменных
            csp_connect = settings.CSP_CONNECT_SRC
            csp_script = settings.CSP_SCRIPT_SRC
            response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src {csp_script}; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src {csp_connect}"
            
            return response
            
        except Exception as e:
            process_time = time.time() - start_time
            logger.error(f"Request failed: {str(e)}", 
                        extra={
                            "method": request.method,
                            "path": str(request.url.path),
                            "client_ip": client_ip,
                            "process_time": round(process_time, 3),
                            "correlation_id": correlation_id,
                            "error": str(e)
                        })
            raise
    
    def _get_client_ip(self, request: Request) -> str:
        """Получает реальный IP клиента"""
        # Проверяем заголовки от прокси
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Берем первый IP из списка
            return forwarded_for.split(',')[0].strip()
        
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        cf_connecting_ip = request.headers.get("CF-Connecting-IP")
        if cf_connecting_ip:
            return cf_connecting_ip
        
        return request.client.host if request.client else "unknown"
    
    def _is_suspicious_request(self, request: Request) -> bool:
        """Определяет подозрительные запросы"""
        user_agent = request.headers.get("user-agent", "").lower()
        path = str(request.url.path).lower()
        query = str(request.url.query).lower()
        
        # Проверяем User-Agent
        for suspicious_ua in self.suspicious_user_agents:
            if suspicious_ua in user_agent:
                return True
        
        # Проверяем на SQL injection паттерны
        sql_patterns = [
            "union select", "' or '1'='1", "admin'--", "' or 1=1",
            "select * from", "drop table", "insert into"
        ]
        
        for pattern in sql_patterns:
            if pattern in path or pattern in query:
                return True
        
        # Проверяем на XSS паттерны
        xss_patterns = [
            "<script>", "javascript:", "onload=", "onerror=",
            "alert(", "eval(", "document.cookie"
        ]
        
        for pattern in xss_patterns:
            if pattern in path or pattern in query:
                return True
        
        # Проверяем на path traversal
        if "../" in path or "..%2f" in path or "..%5c" in path:
            return True
        
        return False

class CircuitBreaker:
    """Circuit breaker для внешних сервисов"""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half_open
    
    def call(self, func, *args, **kwargs):
        """Выполняет функцию с circuit breaker protection"""
        if self.state == "open":
            if time.time() - self.last_failure_time > self.timeout:
                self.state = "half_open"
                logger.info("Circuit breaker moving to half-open state")
            else:
                raise HTTPException(status_code=503, detail="Service temporarily unavailable")
        
        try:
            result = func(*args, **kwargs)
            
            if self.state == "half_open":
                self.state = "closed"
                self.failure_count = 0
                logger.info("Circuit breaker closed - service recovered")
            
            return result
            
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                logger.error(f"Circuit breaker opened due to {self.failure_count} failures")
            
            raise e

# Глобальные circuit breakers для внешних сервисов
payment_circuit_breaker = CircuitBreaker(failure_threshold=3, timeout=30)
supabase_circuit_breaker = CircuitBreaker(failure_threshold=5, timeout=60) 