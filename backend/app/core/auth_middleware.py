import json
import hmac
import hashlib
import time
from typing import Optional

import httpx
from fastapi import Request
from starlette.responses import JSONResponse
from jose import jwt

from app.core.config import settings


class JWKSCache:
    def __init__(self):
        self.jwks: Optional[dict] = None
        self.fetched_at: float = 0.0
        self.ttl_seconds: int = 3600

    async def get_jwks(self) -> dict:
        now = time.time()
        if self.jwks and (now - self.fetched_at) < self.ttl_seconds:
            return self.jwks
        jwks_url = settings.SUPABASE_JWKS_URL or f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        headers = {
            "apikey": settings.SUPABASE_ANON_KEY
        }
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(jwks_url, headers=headers)
            resp.raise_for_status()
            self.jwks = resp.json()
            self.fetched_at = now
            return self.jwks


jwks_cache = JWKSCache()


class AuthMiddleware:
    """Middleware аутентификации клиента.

    Порядок:
    1) Пытается проверить Supabase JWT из Authorization: Bearer <token>
    2) Переходный фоллбек: X-Client-Id + X-Client-Timestamp + X-Client-Signature (HMAC-SHA256)
    Успешно найденный client_id кладет в request.state.client_id и request.state.auth_method
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)

        # 🔍 DEBUG: Логируем ВСЕ входящие запросы для диагностики APK проблемы
        import logging
        logger = logging.getLogger(__name__)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        origin = request.headers.get("origin", "NO_ORIGIN")
        user_agent = request.headers.get("user-agent", "NO_UA")
        logger.info(f"🔍 AUTH_MIDDLEWARE: {method} {path} | Origin: {origin} | UA: {user_agent[:50]}")

        client_id: Optional[str] = None
        auth_method: Optional[str] = None

        # 1) JWT путь
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            try:
                # Проверяем алгоритм токена
                unverified_header = jwt.get_unverified_header(token)
                alg = unverified_header.get("alg", "HS256")

                # HS256 (legacy JWT secret) - используем shared secret
                if alg == "HS256":
                    if not settings.SUPABASE_JWT_SECRET:
                        return await self._unauthorized(scope, receive, send, "unauthorized", "JWT secret not configured")

                    options = {"verify_aud": bool(settings.JWT_VERIFY_AUD)}
                    audience = settings.JWT_VERIFY_AUD or None
                    issuer = settings.JWT_VERIFY_ISS or None
                    payload = jwt.decode(
                        token,
                        settings.SUPABASE_JWT_SECRET,
                        algorithms=["HS256"],
                        audience=audience,
                        options=options,
                        issuer=issuer if issuer else None,
                    )
                else:
                    # RS256/ES256 - используем JWKS для безопасности
                    jwks = await jwks_cache.get_jwks()
                    kid = unverified_header.get("kid")
                    key = None
                    for k in jwks.get("keys", []):
                        if k.get("kid") == kid:
                            key = k
                            break
                    if key is None and jwks.get("keys"):
                        # Иногда kid может не совпадать/отсутствовать — пробуем первый ключ
                        key = jwks["keys"][0]
                    if not key:
                        return await self._unauthorized(scope, receive, send, "unauthorized", "JWKS key not found")

                    options = {"verify_aud": bool(settings.JWT_VERIFY_AUD)}
                    audience = settings.JWT_VERIFY_AUD or None
                    issuer = settings.JWT_VERIFY_ISS or None
                    payload = jwt.decode(
                        token,
                        key,
                        algorithms=[key.get("alg", "RS256"), "RS256", "ES256"],
                        audience=audience,
                        options=options,
                        issuer=issuer if issuer else None,
                    )

                client_id = (
                    str(payload.get("sub"))
                    or str((payload.get("user_metadata") or {}).get("client_id"))
                )
                if client_id:
                    scope.setdefault("state", {})["client_id"] = client_id
                    scope["state"]["auth_method"] = "jwt"
                    await self.app(scope, receive, send)
                    return
            except Exception as e:
                # ВРЕМЕННО: Логируем ошибку JWT валидации
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"❌ JWT validation failed: {e}", exc_info=True)
                # Падаем в фоллбек ниже
                pass

        # 2) Переходный HMAC фоллбек
        x_client_id = request.headers.get("x-client-id")
        x_ts = request.headers.get("x-client-timestamp")
        x_sig = request.headers.get("x-client-signature")
        secret = settings.CLIENT_FALLBACK_SECRET
        if x_client_id and x_ts and x_sig and secret:
            try:
                # проверка дрейфа времени <= 5 минут
                now_ms = int(time.time() * 1000)
                ts_ms = int(x_ts)
                if abs(now_ms - ts_ms) > 5 * 60 * 1000:
                    return await self._unauthorized(scope, receive, send, "unauthorized", "timestamp_drift")

                msg = f"{x_client_id}.{x_ts}".encode()
                expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, x_sig):
                    return await self._unauthorized(scope, receive, send, "unauthorized", "invalid_signature")

                client_id = x_client_id
                scope.setdefault("state", {})["client_id"] = client_id
                scope["state"]["auth_method"] = "hmac"
                await self.app(scope, receive, send)
                return
            except Exception:
                return await self._unauthorized(scope, receive, send, "unauthorized", "invalid_fallback_headers")

        # Если требуются защищенные эндпоинты — возвращаем 401.
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()

        # Публичные endpoints (доступны без аутентификации)
        PUBLIC_PATHS = [
            "/health",
            "/readyz",
            "/health-force",
            "/",
            "/api/v1/locations",  # Список локаций для карты
            "/api/v1/station/status",  # Статус станции для карты
        ]

        # Проверяем публичные endpoints (точное совпадение или начало пути)
        is_public = False
        for public_path in PUBLIC_PATHS:
            if path == public_path or (public_path.endswith("s") and path.startswith(public_path)):
                is_public = True
                break

        # Дополнительно: разрешаем GET для /api/v1/locations/{id}
        if path.startswith("/api/v1/locations/") and method == "GET":
            is_public = True

        # Дополнительно: разрешаем GET для /api/v1/station/status/{station_id}
        if path.startswith("/api/v1/station/status/") and method == "GET":
            is_public = True

        # Если не публичный endpoint, требуем аутентификацию
        if not is_public:
            # Требуем auth для ВСЕХ /api/v1/* (кроме публичных выше)
            # И для мутирующих методов на любых путях
            if path.startswith("/api/v1") or method in ("POST", "PUT", "DELETE"):
                return await self._unauthorized(scope, receive, send, "unauthorized", "missing_token")

        await self.app(scope, receive, send)

    async def _unauthorized(self, scope, receive, send, error: str, message: str):
        # 🔍 DEBUG: Логируем все 401 ответы для диагностики APK проблемы
        import logging
        logger = logging.getLogger(__name__)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        logger.warning(f"🚫 AUTH_BLOCKED: {method} {path} | Reason: {error} - {message}")

        response = JSONResponse(
            status_code=401,
            content={"success": False, "error": error, "message": message},
        )
        await response(scope, receive, send)


