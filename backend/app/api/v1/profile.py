from fastapi import APIRouter, Depends, Request
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
        "client_id": row.id,
        "email": row.email,
        "phone": row.phone,
        "name": row.name,
        "balance": float(row.balance or 0),
        "status": row.status,
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


