"""
Cookie-based аутентификация поверх Supabase.
"""
from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from starlette.responses import JSONResponse
import httpx
import os
from datetime import timedelta, datetime, timezone
from typing import Optional, Dict, Any
from jose import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session
import logging

from app.core.config import settings
from app.db.session import get_db

logger = logging.getLogger("app.api.v1.auth.session")

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=5, max_length=32)
    password: str

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return v.strip()

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: EmailStr | None) -> EmailStr | None:
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not v:
            raise ValueError("password is required")
        return v

    @property
    def is_email_flow(self) -> bool:
        return self.email is not None

    @property
    def is_phone_flow(self) -> bool:
        return self.phone is not None

    @model_validator(mode="after")
    def ensure_email_or_phone(self) -> "LoginRequest":
        if not self.email and not self.phone:
            raise ValueError("either email or phone must be provided")
        return self


def _cookie_params(ttl_seconds: int, strict: bool = False, samesite: Optional[str] = None, request: Optional[Request] = None):
    """
    Генерирует параметры для установки cookie с автоматической адаптацией для localhost.

    Args:
        ttl_seconds: Время жизни cookie в секундах
        strict: Если True, использовать более строгий SameSite
        samesite: Явное указание SameSite (если None - автоопределение)
        request: Request объект для определения origin (опционально)

    Returns:
        dict: Параметры для set_cookie()

    Notes:
        - Для localhost: SameSite=Lax, Secure=False (разрешает HTTP)
        - Для production: SameSite=None, Secure=True (только HTTPS)
    """
    # Определяем окружение по origin
    is_localhost = False
    if request:
        origin = request.headers.get("origin", "")
        is_localhost = "localhost" in origin or "127.0.0.1" in origin

    # Адаптируем параметры под окружение
    if is_localhost:
        # Для localhost: SameSite=Lax работает с HTTP
        same_site = "lax"
        secure = False
        logger.info("Cookie params: localhost mode (SameSite=Lax, Secure=False)")
    else:
        # Для production: SameSite=None требует Secure (HTTPS)
        same_site = (samesite or ("strict" if strict else "none")).lower()
        secure = True

    return {
        "httponly": True,
        "secure": secure,
        "samesite": same_site,
        "domain": os.getenv("COOKIE_DOMAIN", ".evpower.kg"),
        "path": "/",
        "max_age": ttl_seconds,
        "expires": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    }


ACCESS_TOKEN_EXPIRE_SECONDS = 10 * 60  # 10 минут
REFRESH_TOKEN_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 дней


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _mint_jwt(subject: str, ttl_seconds: int, token_type: str) -> str:
    now = _now_ts()
    payload: Dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + ttl_seconds,
        "typ": token_type,
        "iss": "evpower-backend",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def create_access_token(user_id: str) -> str:
    return _mint_jwt(user_id, ACCESS_TOKEN_EXPIRE_SECONDS, "access")


def create_refresh_token(user_id: str) -> str:
    return _mint_jwt(user_id, REFRESH_TOKEN_EXPIRE_SECONDS, "refresh")


@router.get("/csrf")
async def get_csrf(request: Request):
    """
    Получение CSRF токена для защиты от CSRF атак.

    Использует double-submit cookie pattern:
    - Токен в cookie (XSRF-TOKEN) - автоматически отправляется браузером
    - Токен в response body - фронтенд должен отправить в заголовке X-CSRF-Token

    Cookie не HttpOnly, чтобы JavaScript мог прочитать и отправить в заголовке.
    """
    # Если токен уже есть в cookie, переиспользуем его, чтобы не ломать параллельные/повторные запросы
    existing = request.cookies.get("XSRF-TOKEN")
    token = existing if existing else os.urandom(16).hex()
    resp = JSONResponse({"success": True, "csrf_token": token})

    # Определяем параметры cookie в зависимости от окружения
    origin = request.headers.get("origin", "")
    is_localhost = "localhost" in origin or "127.0.0.1" in origin

    # Не HttpOnly, чтобы фронт мог прочитать и пробросить в X-CSRF-Token
    resp.set_cookie(
        "XSRF-TOKEN",
        token,
        httponly=False,
        secure=False if is_localhost else True,
        samesite="lax" if is_localhost else "none",
        domain=os.getenv("COOKIE_DOMAIN", ".evpower.kg"),
        path="/",
        max_age=60 * 60,  # 1 час
    )
    return resp


@router.get("/cierra")
async def get_csrf_alias(http_request: Request):
    # Alias для фронта: полностью идентично /csrf
    return await get_csrf(http_request)


@router.get("/me")
async def get_me(request: Request, db: Session = Depends(get_db)):
    """
    Получение данных текущего аутентифицированного пользователя.

    Стандартный REST endpoint (алиас для /api/v1/profile).
    Автоматически определяет client_id из токена аутентификации.

    Returns:
        dict: Данные пользователя (client_id, email, phone, name, balance, status)

    Raises:
        401: Если пользователь не аутентифицирован
        404: Если пользователь не найден в БД
    """
    client_id = getattr(request.state, "client_id", None)

    if not client_id:
        logger.warning("Попытка получить /auth/me без аутентификации")
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated", "status": 401}
        )

    # Получаем данные пользователя из БД
    try:
        row = db.execute(
            text("SELECT id, email, phone, name, balance, status FROM clients WHERE id = :id"),
            {"id": client_id}
        ).fetchone()

        if not row:
            logger.warning(f"Клиент {client_id} не найден в БД")
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "not_found", "message": "Client not found", "status": 404}
            )

        return {
            "success": True,
            "client_id": row.id,
            "email": row.email,
            "phone": row.phone,
            "name": row.name,
            "balance": float(row.balance or 0),
            "status": row.status,
        }

    except Exception as e:
        logger.error(f"Ошибка получения данных пользователя {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Internal server error", "status": 500}
        )

@router.post("/login")
async def login(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    """
    Логин через Supabase (password grant) с установкой cookie evp_access/evp_refresh.
    """
    try:
        # CSRF: проверяем доверенный Origin и совпадение заголовка с cookie
        origin = request.headers.get("origin")
        trusted = [o.strip() for o in settings.CSRF_TRUSTED_ORIGINS.split(",") if o.strip()]
        if not origin or origin not in trusted:
            logger.warning("CSRF origin rejected", extra={"origin": origin})
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "csrf_error", "message": "Untrusted origin", "status": 401},
            )
        header_token = request.headers.get("X-CSRF-Token")
        cookie_token = request.cookies.get("XSRF-TOKEN")
        if not header_token or not cookie_token or header_token != cookie_token:
            logger.warning(
                "CSRF token mismatch",
                extra={
                    "has_header": bool(header_token),
                    "has_cookie": bool(cookie_token),
                    "origin": origin,
                },
            )
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "csrf_error", "message": "Invalid CSRF token", "status": 401},
            )

        # 1) Password grant в Supabase
        supabase_url = settings.SUPABASE_URL.rstrip("/")
        token_url = f"{supabase_url}/auth/v1/token?grant_type=password"
        headers = {"apikey": settings.SUPABASE_ANON_KEY, "Content-Type": "application/json"}

        # Если пришёл телефон, находим email в public.clients для последующей аутентификации
        # Supabase не поддерживает grant_type=password с телефоном (только email)
        login_email: Optional[str] = None
        if body.is_email_flow:
            login_email = str(body.email)
            logger.info("Login attempt with email", extra={"email": login_email})
        elif body.is_phone_flow and body.phone:
            # Ищем email по телефону в public.clients (там phone всегда заполнен)
            try:
                result = db.execute(
                    text("SELECT email FROM public.clients WHERE phone = :phone LIMIT 1"),
                    {"phone": body.phone},
                ).fetchone()
                if result and result[0]:
                    login_email = result[0]
                    logger.info(
                        "Phone lookup successful",
                        extra={"phone": body.phone, "resolved_email": login_email}
                    )
                else:
                    logger.warning(
                        "Phone not found in database",
                        extra={"phone": body.phone}
                    )
            except Exception as e:
                logger.exception(
                    "Failed to map phone to email via DB",
                    extra={"phone": body.phone, "error": str(e)}
                )
                login_email = None

        async with httpx.AsyncClient(timeout=10) as client:
            # 1a) try phone login if phone present
            if body.is_phone_flow and body.phone:
                phone_payload: dict = {"password": body.password, "phone": body.phone}
                pr = await client.post(token_url, headers=headers, json=phone_payload)
                logger.info("Supabase phone login attempt", extra={"status": pr.status_code})
                if pr.status_code == 200:
                    r = pr
                else:
                    # 1b) fallback to email if we resolved one
                    if login_email:
                        payload: dict = {"password": body.password, "email": login_email}
                        r = await client.post(token_url, headers=headers, json=payload)
                        logger.info("Supabase email fallback attempt", extra={"status": r.status_code})
                    else:
                        r = pr  # keep last response
            else:
                payload: dict = {"password": body.password, "email": login_email}
                r = await client.post(token_url, headers=headers, json=payload)
                logger.info("Supabase email login attempt", extra={"status": r.status_code})
        if r.status_code != 200:
            try:
                err_body = r.json()
            except Exception:
                err_body = {"_": "non-json"}
            logger.warning("Supabase password grant rejected", extra={"status": r.status_code, "body": err_body})
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "invalid_credentials", "message": "Неверный логин или пароль", "status": 401},
            )
        data = r.json()
        supa_access = data.get("access_token")
        if not supa_access:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "auth_provider_error", "message": "Не удалось получить access_token", "status": 500},
            )

        # 2) Получаем user.id у Supabase (нужен subject для наших JWT)
        async with httpx.AsyncClient(timeout=10) as client:
            ur = await client.get(
                f"{supabase_url}/auth/v1/user",
                headers={"apikey": settings.SUPABASE_ANON_KEY, "Authorization": f"Bearer {supa_access}"},
            )
        if ur.status_code != 200:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "auth_provider_error", "message": "Не удалось получить пользователя", "status": 500},
            )
        user_json = ur.json()
        user_id: Optional[str] = user_json.get("id") or (user_json.get("user") or {}).get("id")
        if not user_id:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "auth_provider_error", "message": "Не удалось определить user_id", "status": 500},
            )

        # 3) Минтим НАШИ токены и кладём в cookie
        access_token = create_access_token(user_id)
        refresh_token = create_refresh_token(user_id)

        resp = JSONResponse({"success": True})

        # ВАЖНО: Очищаем старые cookies с Domain=ocpp.evpower.kg (до изменения на .evpower.kg)
        # Это нужно для совместимости после редеплоя с новым COOKIE_DOMAIN
        for cookie_name in ("evp_access", "evp_refresh", "XSRF-TOKEN"):
            resp.set_cookie(
                cookie_name,
                "",
                httponly=(cookie_name != "XSRF-TOKEN"),
                secure=True,
                samesite="none",
                domain="ocpp.evpower.kg",  # Старый domain (без точки)
                path="/",
                max_age=0,  # Удаляем
            )

        # Устанавливаем НОВЫЕ cookies с Domain=.evpower.kg (cross-subdomain)
        # evp_access ~10 минут, evp_refresh ~7 дней
        resp.set_cookie("evp_access", access_token, **_cookie_params(10 * 60, samesite="none", request=request))
        resp.set_cookie("evp_refresh", refresh_token, **_cookie_params(7 * 24 * 3600, samesite="none", request=request))
        return resp
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": str(e), "status": 500},
        )


@router.post("/refresh")
async def refresh(request: Request):
    """
    Ротация refresh: по НАШЕМУ cookie evp_refresh выдаем новую пару токенов.
    При невалидном/просроченном refresh — 401.
    """
    try:
        refresh_cookie = request.cookies.get("evp_refresh")
        if not refresh_cookie:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "unauthorized", "message": "Missing refresh token", "status": 401},
            )

        # Декодируем наш refresh
        try:
            payload = jwt.decode(refresh_cookie, settings.SECRET_KEY, algorithms=["HS256"], options={"verify_aud": False})
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "unauthorized", "message": "Invalid refresh token", "status": 401},
            )
        if payload.get("typ") != "refresh":
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "unauthorized", "message": "Invalid token type", "status": 401},
            )
        user_id = payload.get("sub")
        if not user_id:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "unauthorized", "message": "Invalid subject", "status": 401},
            )

        # Минтим новую пару
        access_token = create_access_token(user_id)
        new_refresh = create_refresh_token(user_id)
        resp = JSONResponse({"success": True})
        # ВАЖНО: используем samesite="none" для cross-subdomain cookies (app.evpower.kg → ocpp.evpower.kg)
        # SameSite=Strict блокирует отправку cookies при cross-site запросах после перезагрузки страницы
        resp.set_cookie("evp_access", access_token, **_cookie_params(10 * 60, samesite="none", request=request))
        resp.set_cookie("evp_refresh", new_refresh, **_cookie_params(7 * 24 * 3600, samesite="none", request=request))
        return resp
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": str(e), "status": 500},
        )


@router.post("/logout")
async def logout():
    """
    Идемпотентный logout: чистим cookies.
    """
    resp = JSONResponse({"success": True})
    # Очистка: max_age=0
    for name in ("evp_access", "evp_refresh", "XSRF-TOKEN"):
        resp.set_cookie(
            name,
            "",
            httponly=(name != "XSRF-TOKEN"),
            secure=True,
            samesite="lax",
            domain=os.getenv("COOKIE_DOMAIN", ".evpower.kg"),
            path="/",
            max_age=0,
        )
    return resp


