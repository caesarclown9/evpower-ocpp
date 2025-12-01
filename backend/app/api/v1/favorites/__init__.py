"""
Favorites API endpoints - управление избранными локациями.

PWA ожидает следующие эндпоинты:
- GET /api/v1/favorites - список избранных location_id
- POST /api/v1/favorites - добавить в избранное (body: {location_id})
- DELETE /api/v1/favorites/{location_id} - удалить из избранного
- GET /api/v1/favorites/{location_id}/check - проверить статус
- POST /api/v1/favorites/{location_id}/toggle - переключить статус
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
import uuid

from app.db.session import get_db

router = APIRouter(prefix="/favorites")


class AddFavoriteRequest(BaseModel):
    location_id: str


@router.get("")
async def get_favorites(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Получить список избранных локаций пользователя.

    PWA ожидает формат:
    {
        "success": true,
        "favorites": ["location_id1", "location_id2", ...]
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
        SELECT location_id
        FROM user_favorites
        WHERE user_id = :user_id
        ORDER BY created_at DESC
    """)

    rows = db.execute(query, {"user_id": client_id}).fetchall()
    favorites = [row.location_id for row in rows]

    return {
        "success": True,
        "favorites": favorites
    }


@router.post("")
async def add_favorite(
    request: Request,
    body: AddFavoriteRequest,
    db: Session = Depends(get_db),
):
    """
    Добавить локацию в избранное.

    PWA ожидает формат:
    {
        "success": true,
        "already_exists": boolean (optional)
    }
    """
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    # Проверяем, существует ли уже запись
    existing = db.execute(
        text("""
            SELECT id FROM user_favorites
            WHERE user_id = :user_id AND location_id = :location_id
        """),
        {"user_id": client_id, "location_id": body.location_id}
    ).fetchone()

    if existing:
        return {
            "success": True,
            "already_exists": True
        }

    # Добавляем новую запись
    new_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO user_favorites (id, user_id, location_id, created_at)
            VALUES (:id, :user_id, :location_id, NOW())
        """),
        {
            "id": new_id,
            "user_id": client_id,
            "location_id": body.location_id
        }
    )
    db.commit()

    return {
        "success": True,
        "already_exists": False
    }


@router.delete("/{location_id}")
async def remove_favorite(
    request: Request,
    location_id: str,
    db: Session = Depends(get_db),
):
    """
    Удалить локацию из избранного.

    PWA ожидает формат:
    {
        "success": true
    }
    """
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    db.execute(
        text("""
            DELETE FROM user_favorites
            WHERE user_id = :user_id AND location_id = :location_id
        """),
        {"user_id": client_id, "location_id": location_id}
    )
    db.commit()

    return {"success": True}


@router.get("/{location_id}/check")
async def check_favorite(
    request: Request,
    location_id: str,
    db: Session = Depends(get_db),
):
    """
    Проверить, является ли локация избранной.

    PWA ожидает формат:
    {
        "success": true,
        "is_favorite": boolean
    }
    """
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    existing = db.execute(
        text("""
            SELECT id FROM user_favorites
            WHERE user_id = :user_id AND location_id = :location_id
        """),
        {"user_id": client_id, "location_id": location_id}
    ).fetchone()

    return {
        "success": True,
        "is_favorite": existing is not None
    }


@router.post("/{location_id}/toggle")
async def toggle_favorite(
    request: Request,
    location_id: str,
    db: Session = Depends(get_db),
):
    """
    Переключить статус избранного.

    PWA ожидает формат:
    {
        "success": true,
        "is_favorite": boolean,
        "action": "added" | "removed"
    }
    """
    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return {
            "success": False,
            "error": "unauthorized",
            "message": "Missing or invalid authentication"
        }

    # Проверяем текущий статус
    existing = db.execute(
        text("""
            SELECT id FROM user_favorites
            WHERE user_id = :user_id AND location_id = :location_id
        """),
        {"user_id": client_id, "location_id": location_id}
    ).fetchone()

    if existing:
        # Удаляем из избранного
        db.execute(
            text("""
                DELETE FROM user_favorites
                WHERE user_id = :user_id AND location_id = :location_id
            """),
            {"user_id": client_id, "location_id": location_id}
        )
        db.commit()
        return {
            "success": True,
            "is_favorite": False,
            "action": "removed"
        }
    else:
        # Добавляем в избранное
        new_id = str(uuid.uuid4())
        db.execute(
            text("""
                INSERT INTO user_favorites (id, user_id, location_id, created_at)
                VALUES (:id, :user_id, :location_id, NOW())
            """),
            {
                "id": new_id,
                "user_id": client_id,
                "location_id": location_id
            }
        )
        db.commit()
        return {
            "success": True,
            "is_favorite": True,
            "action": "added"
        }
