"""
Cookie-based аутентификация поверх Supabase.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from starlette.responses import JSONResponse
import httpx
import os
from datetime import timedelta, datetime, timezone
from typing import Optional, Dict, Any
from jose import jwt

from app.core.config import settings

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


def _cookie_params(ttl_seconds: int, strict: bool):
    same_site = "strict" if strict else "lax"
    return {
        "httponly": True,
        "secure": True,
        "samesite": same_site,  # evp_access=Lax, evp_refresh=Strict
        "domain": os.getenv("COOKIE_DOMAIN", "ocpp.evpower.kg"),
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
async def get_csrf():
    token = os.urandom(16).hex()
    resp = JSONResponse({"success": True, "csrf_token": token})
    # Не HttpOnly, чтобы фронт мог прочитать и пробросить в X-CSRF-Token
    resp.set_cookie(
        "XSRF-TOKEN",
        token,
        httponly=False,
        secure=True,
        samesite="lax",
        domain=os.getenv("COOKIE_DOMAIN", "ocpp.evpower.kg"),
        path="/",
        max_age=60 * 60,  # 1 час
    )
    return resp


@router.get("/cierra")
async def get_csrf_alias():
    # Alias для фронта: полностью идентично /csrf
    return await get_csrf()

@router.post("/login")
async def login(request: Request, body: LoginRequest):
    """
    Логин через Supabase (password grant) с установкой cookie evp_access/evp_refresh.
    """
    try:
        # CSRF: проверяем доверенный Origin и совпадение заголовка с cookie
        origin = request.headers.get("origin")
        trusted = [o.strip() for o in settings.CSRF_TRUSTED_ORIGINS.split(",") if o.strip()]
        if not origin or origin not in trusted:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "csrf_error", "message": "Untrusted origin", "status": 401},
            )
        header_token = request.headers.get("X-CSRF-Token")
        cookie_token = request.cookies.get("XSRF-TOKEN")
        if not header_token or not cookie_token or header_token != cookie_token:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "csrf_error", "message": "Invalid CSRF token", "status": 401},
            )

        # 1) Password grant в Supabase
        supabase_url = settings.SUPABASE_URL.rstrip("/")
        token_url = f"{supabase_url}/auth/v1/token?grant_type=password"
        headers = {"apikey": settings.SUPABASE_ANON_KEY, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            payload: dict = {"password": body.password}
            if body.is_email_flow:
                payload["email"] = str(body.email)
            elif body.is_phone_flow:
                payload["phone"] = body.phone
            r = await client.post(token_url, headers=headers, json=payload)
        if r.status_code != 200:
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
        # evp_access ~10 минут, evp_refresh ~7 дней
        resp.set_cookie("evp_access", access_token, **_cookie_params(10 * 60, strict=False))
        resp.set_cookie("evp_refresh", refresh_token, **_cookie_params(7 * 24 * 3600, strict=True))
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
        resp.set_cookie("evp_access", access_token, **_cookie_params(10 * 60, strict=False))
        resp.set_cookie("evp_refresh", new_refresh, **_cookie_params(7 * 24 * 3600, strict=True))
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
            domain=os.getenv("COOKIE_DOMAIN", "ocpp.evpower.kg"),
            path="/",
            max_age=0,
        )
    return resp


