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
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {"success": False, "error": "unauthorized", "message": "Missing or invalid authentication"}

    row = db.execute(text("SELECT id, email, phone, name, balance, status FROM clients WHERE id = :id"), {"id": client_id}).fetchone()
    if not row:
        return {"success": False, "error": "not_found", "message": "Client not found"}

    return {
        "success": True,
        "data": {
            "id": row.id,
            "email": row.email,
            "phone": row.phone,
            "name": row.name,
            "balance": float(row.balance or 0),
            "status": row.status,
        }
    }


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
        return {"success": False, "error": "internal_error", "message": str(e)}

