"""
History API endpoints - история зарядок и транзакций.

PWA ожидает следующие эндпоинты:
- GET /api/v1/history/charging - история зарядок
- GET /api/v1/history/transactions - история транзакций
"""
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional

from app.db.session import get_db

router = APIRouter(prefix="/history")


@router.get("/charging")
async def get_charging_history(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    Получить историю зарядок пользователя.

    PWA ожидает формат:
    {
        "success": true,
        "data": [{
            "id": "string",
            "station_id": "string | null",
            "connector_id": number | null,
            "status": "string | null",
            "energy_kwh": number,
            "amount": number,
            "started_at": "string | null",
            "ended_at": "string | null",
            "duration_minutes": number,
            "limit_type": "string",
            "limit_value": number,
            "station": {
                "model": "string | null",
                "location": {
                    "name": "string | null",
                    "address": "string | null"
                } | null
            } | null
        }],
        "total": number,
        "limit": number,
        "offset": number
    }
    """
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    # Получаем общее количество записей
    count_result = db.execute(
        text("SELECT COUNT(*) FROM charging_sessions WHERE user_id = :user_id"),
        {"user_id": client_id}
    ).scalar()
    total = count_result or 0

    # Получаем историю зарядок с JOIN на stations и locations
    query = text("""
        SELECT
            cs.id,
            cs.station_id,
            cs.status,
            cs.energy as energy_kwh,
            cs.amount,
            cs.start_time as started_at,
            cs.stop_time as ended_at,
            cs.limit_type,
            cs.limit_value,
            s.model as station_model,
            l.name as location_name,
            l.address as location_address
        FROM charging_sessions cs
        LEFT JOIN stations s ON cs.station_id = s.id
        LEFT JOIN locations l ON s.location_id = l.id
        WHERE cs.user_id = :user_id
        ORDER BY cs.start_time DESC NULLS LAST, cs.created_at DESC
        LIMIT :limit OFFSET :offset
    """)

    rows = db.execute(query, {
        "user_id": client_id,
        "limit": limit,
        "offset": offset
    }).fetchall()

    data = []
    for row in rows:
        # Вычисляем duration_minutes
        duration_minutes = 0
        if row.started_at and row.ended_at:
            duration_seconds = (row.ended_at - row.started_at).total_seconds()
            duration_minutes = int(duration_seconds / 60)

        # Формируем station object
        station = None
        if row.station_id:
            location = None
            if row.location_name or row.location_address:
                location = {
                    "name": row.location_name,
                    "address": row.location_address
                }
            station = {
                "model": row.station_model,
                "location": location
            }

        data.append({
            "id": row.id,
            "station_id": row.station_id,
            "connector_id": 1,  # TODO: добавить connector_id в charging_sessions
            "status": row.status,
            "energy_kwh": float(row.energy_kwh or 0),
            "amount": float(row.amount or 0),
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            "duration_minutes": duration_minutes,
            "limit_type": row.limit_type,
            "limit_value": float(row.limit_value) if row.limit_value else None,
            "station": station
        })

    return {
        "success": True,
        "data": data,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.get("/transactions")
async def get_transaction_history(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    Получить историю транзакций пользователя.

    PWA ожидает формат:
    {
        "success": true,
        "data": [{
            "id": "string",
            "transaction_type": "string",
            "amount": number,
            "requested_amount": number,
            "balance_before": number | null,
            "balance_after": number | null,
            "created_at": "string | null",
            "completed_at": "string | null",
            "status": "string | null",
            "payment_method": "string | null",
            "invoice_id": "string | null"
        }],
        "total": number,
        "limit": number,
        "offset": number
    }
    """
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    # Получаем общее количество записей
    count_result = db.execute(
        text("SELECT COUNT(*) FROM payment_transactions_odengi WHERE client_id = :client_id"),
        {"client_id": client_id}
    ).scalar()
    total = count_result or 0

    # Получаем историю транзакций
    query = text("""
        SELECT
            pt.id,
            pt.transaction_type,
            pt.amount,
            pt.balance_before,
            pt.balance_after,
            pt.created_at,
            pt.charging_session_id,
            bt.status as topup_status,
            bt.payment_provider,
            bt.invoice_id,
            bt.requested_amount
        FROM payment_transactions_odengi pt
        LEFT JOIN balance_topups bt ON pt.balance_topup_id = bt.id
        WHERE pt.client_id = :client_id
        ORDER BY pt.created_at DESC
        LIMIT :limit OFFSET :offset
    """)

    rows = db.execute(query, {
        "client_id": client_id,
        "limit": limit,
        "offset": offset
    }).fetchall()

    data = []
    for row in rows:
        # Определяем статус транзакции
        status = "completed"
        if row.topup_status:
            status = row.topup_status

        data.append({
            "id": str(row.id),
            "transaction_type": row.transaction_type,
            "amount": float(row.amount or 0),
            "requested_amount": float(row.requested_amount or row.amount or 0),
            "balance_before": float(row.balance_before) if row.balance_before is not None else None,
            "balance_after": float(row.balance_after) if row.balance_after is not None else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "completed_at": row.created_at.isoformat() if row.created_at else None,
            "status": status,
            "payment_method": row.payment_provider,
            "invoice_id": row.invoice_id
        })

    return {
        "success": True,
        "data": data,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.get("/stats")
async def get_charging_stats(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Получить статистику зарядок пользователя.

    PWA ожидает формат:
    {
        "success": true,
        "stats": {
            "total_sessions": number,
            "total_energy_kwh": number,
            "total_amount": number,
            "average_session_minutes": number
        }
    }
    """
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    query = text("""
        SELECT
            COUNT(*) as total_sessions,
            COALESCE(SUM(energy), 0) as total_energy_kwh,
            COALESCE(SUM(amount), 0) as total_amount,
            COALESCE(
                AVG(
                    EXTRACT(EPOCH FROM (stop_time - start_time)) / 60
                ),
                0
            ) as average_session_minutes
        FROM charging_sessions
        WHERE user_id = :user_id
          AND start_time IS NOT NULL
          AND stop_time IS NOT NULL
    """)

    row = db.execute(query, {"user_id": client_id}).fetchone()

    return {
        "success": True,
        "stats": {
            "total_sessions": int(row.total_sessions or 0),
            "total_energy_kwh": float(row.total_energy_kwh or 0),
            "total_amount": float(row.total_amount or 0),
            "average_session_minutes": float(row.average_session_minutes or 0)
        }
    }
