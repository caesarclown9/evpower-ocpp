"""
History API endpoints - история зарядок и транзакций.

Используется cookie-based auth (request.state.client_id из AuthMiddleware).
"""
from fastapi import APIRouter, Request, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from starlette.responses import JSONResponse
from typing import List, Optional
from datetime import datetime
import logging

from app.db.session import get_db

logger = logging.getLogger("app.api.v1.history")

router = APIRouter()


def _get_client_id(request: Request) -> str | None:
    """Извлекает client_id из request.state (установлен AuthMiddleware)."""
    return getattr(request.state, "client_id", None)


@router.get("/charging")
async def get_charging_history(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
):
    """
    Получить историю зарядок пользователя.

    Query params:
        limit - количество записей (1-100, default 20)
        offset - смещение для пагинации

    Returns:
        {
            success: true,
            data: [
                {
                    id, station_id, connector_id, status, energy_kwh, amount,
                    started_at, ended_at, duration_minutes,
                    station: { model, location: { name, address } }
                },
                ...
            ],
            total: 42,
            limit: 20,
            offset: 0
        }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        # Получаем общее количество записей
        count_result = db.execute(
            text("SELECT COUNT(*) FROM charging_sessions WHERE user_id = :user_id"),
            {"user_id": client_id}
        ).scalar()

        # Получаем данные с JOIN на stations и locations
        result = db.execute(
            text("""
                SELECT
                    cs.id,
                    cs.station_id,
                    cs.connector_id,
                    cs.status,
                    cs.energy as energy_kwh,
                    cs.amount,
                    cs.created_at as started_at,
                    cs.ended_at,
                    EXTRACT(EPOCH FROM (COALESCE(cs.ended_at, NOW()) - cs.created_at)) / 60 as duration_minutes,
                    s.model as station_model,
                    l.name as location_name,
                    l.address as location_address
                FROM charging_sessions cs
                LEFT JOIN stations s ON cs.station_id = s.id
                LEFT JOIN locations l ON s.location_id = l.id
                WHERE cs.user_id = :user_id
                ORDER BY cs.created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"user_id": client_id, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        for row in result:
            data.append({
                "id": str(row.id),
                "station_id": str(row.station_id) if row.station_id else None,
                "connector_id": row.connector_id,
                "status": row.status,
                "energy_kwh": float(row.energy_kwh or 0),
                "amount": float(row.amount or 0),
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                "duration_minutes": round(row.duration_minutes or 0, 1),
                "station": {
                    "model": row.station_model,
                    "location": {
                        "name": row.location_name,
                        "address": row.location_address
                    }
                } if row.station_model else None
            })

        return {
            "success": True,
            "data": data,
            "total": count_result or 0,
            "limit": limit,
            "offset": offset
        }

    except Exception as e:
        logger.error(f"Error fetching charging history for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to fetch charging history"}
        )


@router.get("/transactions")
async def get_transaction_history(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
):
    """
    Получить историю транзакций (пополнений баланса) пользователя.

    Query params:
        limit - количество записей (1-100, default 20)
        offset - смещение для пагинации

    Returns:
        {
            success: true,
            data: [
                {
                    id, requested_amount, status, payment_method,
                    created_at, completed_at,
                    balance_before, balance_after, amount, transaction_type
                },
                ...
            ],
            total: 15,
            limit: 20,
            offset: 0
        }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        # Получаем общее количество записей
        count_result = db.execute(
            text("SELECT COUNT(*) FROM balance_topups WHERE client_id = :client_id"),
            {"client_id": client_id}
        ).scalar()

        # JOIN balance_topups с payment_transactions_odengi для получения balance_before/after
        result = db.execute(
            text("""
                SELECT
                    bt.id,
                    bt.requested_amount,
                    bt.status,
                    bt.payment_method,
                    bt.created_at,
                    bt.completed_at,
                    bt.invoice_id,
                    pt.balance_before,
                    pt.balance_after,
                    pt.amount as payment_amount,
                    pt.transaction_type
                FROM balance_topups bt
                LEFT JOIN payment_transactions_odengi pt
                    ON pt.balance_topup_id = bt.id
                WHERE bt.client_id = :client_id
                ORDER BY bt.created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"client_id": client_id, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        for row in result:
            # Определяем transaction_type
            if row.transaction_type:
                transaction_type = row.transaction_type
            elif row.status == "canceled":
                transaction_type = "balance_topup_canceled"
            else:
                transaction_type = "balance_topup"

            data.append({
                "id": str(row.id),
                "requested_amount": float(row.requested_amount or 0),
                "status": row.status,
                "payment_method": row.payment_method,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "invoice_id": row.invoice_id,
                "balance_before": float(row.balance_before) if row.balance_before is not None else None,
                "balance_after": float(row.balance_after) if row.balance_after is not None else None,
                "amount": float(row.payment_amount or row.requested_amount or 0),
                "transaction_type": transaction_type
            })

        return {
            "success": True,
            "data": data,
            "total": count_result or 0,
            "limit": limit,
            "offset": offset
        }

    except Exception as e:
        logger.error(f"Error fetching transaction history for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to fetch transaction history"}
        )


@router.get("/stats")
async def get_charging_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Получить статистику зарядок пользователя.

    Returns:
        {
            success: true,
            stats: {
                total_sessions: 42,
                total_energy_kwh: 350.5,
                total_amount: 4200.0,
                average_session_minutes: 45.2
            }
        }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        result = db.execute(
            text("""
                SELECT
                    COUNT(*) as total_sessions,
                    COALESCE(SUM(energy), 0) as total_energy_kwh,
                    COALESCE(SUM(amount), 0) as total_amount,
                    COALESCE(AVG(EXTRACT(EPOCH FROM (COALESCE(ended_at, NOW()) - created_at)) / 60), 0) as avg_duration_minutes
                FROM charging_sessions
                WHERE user_id = :user_id
                AND status IN ('completed', 'finished')
            """),
            {"user_id": client_id}
        ).fetchone()

        return {
            "success": True,
            "stats": {
                "total_sessions": result.total_sessions or 0,
                "total_energy_kwh": round(float(result.total_energy_kwh or 0), 2),
                "total_amount": round(float(result.total_amount or 0), 2),
                "average_session_minutes": round(float(result.avg_duration_minutes or 0), 1)
            }
        }

    except Exception as e:
        logger.error(f"Error fetching charging stats for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to fetch charging stats"}
        )
