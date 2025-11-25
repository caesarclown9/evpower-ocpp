"""
Cookie-based аутентификация поверх Supabase.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, EmailStr
from starlette.responses import JSONResponse
import httpx
import os
from datetime import timedelta, datetime, timezone

from app.core.config import settings

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _cookie_params(ttl_seconds: int, strict: bool):
    same_site = "strict" if strict else "lax"
    return {
        "httponly": True,
        "secure": True,
        "samesite": same_site,  # evp_access=Lax, evp_refresh=Strict
        "domain": os.getenv("COOKIE_DOMAIN", ".evpower.kg"),
        "path": "/",
        "max_age": ttl_seconds,
        "expires": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    }


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
        domain=os.getenv("COOKIE_DOMAIN", ".evpower.kg"),
        path="/",
        max_age=60 * 60,  # 1 час
    )
    return resp


@router.post("/login")
async def login(body: LoginRequest):
    """
    Логин через Supabase (password grant) с установкой cookie evp_access/evp_refresh.
    """
    try:
        supabase_url = settings.SUPABASE_URL.rstrip("/")
        token_url = f"{supabase_url}/auth/v1/token?grant_type=password"
        headers = {"apikey": settings.SUPABASE_ANON_KEY, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(token_url, headers=headers, json={"email": body.email, "password": body.password})
        if r.status_code != 200:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "invalid_credentials", "message": "Неверный логин или пароль", "status": 401},
            )
        data = r.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        if not access_token or not refresh_token:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "auth_provider_error", "message": "Не удалось получить токены", "status": 500},
            )

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
    Ротация refresh: по cookie evp_refresh запрашиваем новые токены в Supabase.
    При невалидном refresh — 401.
    """
    try:
        refresh_token = request.cookies.get("evp_refresh")
        if not refresh_token:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "unauthorized", "message": "Missing refresh token", "status": 401},
            )
        supabase_url = settings.SUPABASE_URL.rstrip("/")
        token_url = f"{supabase_url}/auth/v1/token?grant_type=refresh_token"
        headers = {"apikey": settings.SUPABASE_ANON_KEY, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(token_url, headers=headers, json={"refresh_token": refresh_token})
        if r.status_code != 200:
            # 401 по требованию
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "unauthorized", "message": "Invalid refresh token", "status": 401},
            )
        data = r.json()
        access_token = data.get("access_token")
        new_refresh = data.get("refresh_token")
        resp = JSONResponse({"success": True})
        if access_token:
            resp.set_cookie("evp_access", access_token, **_cookie_params(10 * 60, strict=False))
        if new_refresh:
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
            domain=os.getenv("COOKIE_DOMAIN", ".evpower.kg"),
            path="/",
            max_age=0,
        )
    return resp


