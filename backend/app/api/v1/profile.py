from fastapi import APIRouter, Depends, Request
import httpx
from app.core.config import settings
from app.core.security_middleware import RedisRateLimiter
from app.core.logging_config import correlation_id
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.db.session import get_db

router = APIRouter()


@router.get("/profile")
async def get_profile(request: Request, db: Session = Depends(get_db)):
    """
    Универсальный профиль для clients И users (владельцев станций).

    Гибридный подход: owner также может иметь клиентские данные (баланс, зарядки).
    Возвращает user_type: "client" | "owner" для определения интерфейса на фронте.
    """
    user_id = getattr(request.state, "client_id", None)
    if not user_id:
        return {"success": False, "error": "unauthorized", "message": "Missing or invalid authentication"}

    # 1) Проверяем в users (владельцы станций) — они имеют расширенные права
    owner_row = db.execute(
        text("SELECT id, email, role, is_active FROM users WHERE id = :id"),
        {"id": user_id}
    ).fetchone()

    if owner_row:
        # Owner найден — получаем также клиентские данные если есть
        client_row = db.execute(
            text("SELECT phone, name, balance, status FROM clients WHERE id = :id"),
            {"id": user_id}
        ).fetchone()

        # Получаем количество станций и локаций для владельца
        stats = db.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM stations WHERE user_id = :id) as stations_count,
                    (SELECT COUNT(*) FROM locations WHERE user_id = :id OR admin_id = :id) as locations_count
            """),
            {"id": user_id}
        ).fetchone()

        return {
            "success": True,
            "user_type": "owner",
            "client_id": owner_row.id,
            "user_id": owner_row.id,
            "email": owner_row.email,
            "role": owner_row.role,
            "is_active": owner_row.is_active,
            "stations_count": stats.stations_count if stats else 0,
            "locations_count": stats.locations_count if stats else 0,
            # Клиентские данные (если есть запись в clients)
            "phone": client_row.phone if client_row else None,
            "name": client_row.name if client_row else None,
            "balance": float(client_row.balance or 0) if client_row else 0,
            "status": client_row.status if client_row else "active",
        }

    # 2) Обычный клиент (не owner)
    client_row = db.execute(
        text("SELECT id, email, phone, name, balance, status FROM clients WHERE id = :id"),
        {"id": user_id}
    ).fetchone()

    if client_row:
        return {
            "success": True,
            "user_type": "client",
            "client_id": client_row.id,
            "email": client_row.email,
            "phone": client_row.phone,
            "name": client_row.name,
            "balance": float(client_row.balance or 0),
            "status": client_row.status,
        }

    return {"success": False, "error": "not_found", "message": "User not found"}


@router.post("/account/delete-request")
async def delete_request(request: Request, db: Session = Depends(get_db)):
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {"success": False, "error": "unauthorized", "message": "Missing or invalid authentication"}

    db.execute(text("""
        INSERT INTO balance_audit_log (client_id, attempted_change, success, error_message)
        VALUES (:client_id, 0, false, 'delete_requested')
    """), {"client_id": client_id})
    db.commit()

    # Здесь можно запустить фоновые задачи анонимизации или вызвать RPC Supabase

    return {"success": True, "message": "Удаление аккаунта запрошено"}



@router.post("/auth/logout-all")
async def logout_all_devices(request: Request):
    """Принудительный logout пользователя на всех устройствах через Supabase Admin API"""
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {"success": False, "error": "unauthorized", "message": "Missing or invalid authentication"}

    # Rate limit: не более 5 запросов в час на пользователя
    try:
        limiter = RedisRateLimiter("logout", max_requests=5, window_seconds=3600)
        allowed = await limiter.is_allowed(client_id)
        if not allowed:
            return {"success": False, "error": "too_many_requests", "message": "Logout-all rate limit exceeded"}
    except Exception:
        # fail-open
        pass

    admin_url = f"{settings.SUPABASE_URL}/auth/v1/admin/users/{client_id}/logout"
    headers = {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(admin_url, headers=headers)
            if resp.status_code in (200, 204):
                # Аудит
                try:
                    db = next(get_db())
                    db.execute(text("""
                        INSERT INTO payment_audit_log (
                            request_id, operation_type, client_id, amount,
                            client_ip, request_data, response_data, success, created_at
                        ) VALUES (
                            :request_id, :operation_type, :client_id, :amount,
                            :client_ip, :request_data::jsonb, :response_data::jsonb, :success, NOW()
                        )
                    """), {
                        "request_id": correlation_id.get(),
                        "operation_type": "logout_all",
                        "client_id": client_id,
                        "amount": None,
                        "client_ip": request.client.host if request.client else "unknown",
                        "request_data": "{}",
                        "response_data": "{}",
                        "success": True
                    })
                    db.commit()
                except Exception:
                    pass
                return {"success": True, "message": "Все сессии завершены"}
            return {"success": False, "error": "supabase_error", "status_code": resp.status_code}
    except Exception as e:
        logger.exception("Ошибка при выходе из всех сессий")
        return {"success": False, "error": "internal_error", "message": "Внутренняя ошибка сервера"}

