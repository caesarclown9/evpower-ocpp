"""
Favorites API endpoints - CRUD для избранных станций.

Используется cookie-based auth (request.state.client_id из AuthMiddleware).
"""
from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from starlette.responses import JSONResponse
from typing import List
import logging

from app.db.session import get_db

logger = logging.getLogger("app.api.v1.favorites")

router = APIRouter()


class AddFavoriteRequest(BaseModel):
    location_id: str


class FavoriteItem(BaseModel):
    location_id: str
    created_at: str | None = None


def _get_client_id(request: Request) -> str | None:
    """Извлекает client_id из request.state (установлен AuthMiddleware)."""
    return getattr(request.state, "client_id", None)


@router.get("")
async def get_favorites(request: Request, db: Session = Depends(get_db)):
    """
    Получить список избранных станций пользователя.

    Returns:
        { success: true, favorites: ["location_id_1", "location_id_2", ...] }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        result = db.execute(
            text("SELECT location_id, created_at FROM user_favorites WHERE user_id = :user_id ORDER BY created_at DESC"),
            {"user_id": client_id}
        ).fetchall()

        favorites = [row.location_id for row in result]

        return {
            "success": True,
            "favorites": favorites
        }
    except Exception as e:
        logger.error(f"Error fetching favorites for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to fetch favorites"}
        )


@router.post("")
async def add_favorite(request: Request, body: AddFavoriteRequest, db: Session = Depends(get_db)):
    """
    Добавить станцию в избранное.

    Body:
        { location_id: "..." }

    Returns:
        { success: true } или { success: true, already_exists: true }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        # Проверяем, не существует ли уже
        existing = db.execute(
            text("SELECT id FROM user_favorites WHERE user_id = :user_id AND location_id = :location_id"),
            {"user_id": client_id, "location_id": body.location_id}
        ).fetchone()

        if existing:
            return {"success": True, "already_exists": True}

        # Добавляем новую запись
        db.execute(
            text("""
                INSERT INTO user_favorites (user_id, location_id, created_at, updated_at)
                VALUES (:user_id, :location_id, NOW(), NOW())
            """),
            {"user_id": client_id, "location_id": body.location_id}
        )
        db.commit()

        logger.info(f"User {client_id} added location {body.location_id} to favorites")
        return {"success": True}

    except Exception as e:
        db.rollback()
        logger.error(f"Error adding favorite for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to add favorite"}
        )


@router.delete("/{location_id}")
async def remove_favorite(request: Request, location_id: str, db: Session = Depends(get_db)):
    """
    Удалить станцию из избранного.

    Path params:
        location_id - ID станции для удаления

    Returns:
        { success: true }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        result = db.execute(
            text("DELETE FROM user_favorites WHERE user_id = :user_id AND location_id = :location_id"),
            {"user_id": client_id, "location_id": location_id}
        )
        db.commit()

        if result.rowcount > 0:
            logger.info(f"User {client_id} removed location {location_id} from favorites")

        return {"success": True}

    except Exception as e:
        db.rollback()
        logger.error(f"Error removing favorite for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to remove favorite"}
        )


@router.get("/{location_id}/check")
async def check_favorite(request: Request, location_id: str, db: Session = Depends(get_db)):
    """
    Проверить, является ли станция избранной.

    Path params:
        location_id - ID станции для проверки

    Returns:
        { success: true, is_favorite: true/false }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        result = db.execute(
            text("SELECT id FROM user_favorites WHERE user_id = :user_id AND location_id = :location_id"),
            {"user_id": client_id, "location_id": location_id}
        ).fetchone()

        return {
            "success": True,
            "is_favorite": result is not None
        }

    except Exception as e:
        logger.error(f"Error checking favorite for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to check favorite"}
        )


@router.post("/{location_id}/toggle")
async def toggle_favorite(request: Request, location_id: str, db: Session = Depends(get_db)):
    """
    Переключить статус избранного (добавить если нет, удалить если есть).

    Path params:
        location_id - ID станции

    Returns:
        { success: true, is_favorite: true/false, action: "added"/"removed" }
    """
    client_id = _get_client_id(request)
    if not client_id:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "unauthorized", "message": "Not authenticated"}
        )

    try:
        # Проверяем текущий статус
        existing = db.execute(
            text("SELECT id FROM user_favorites WHERE user_id = :user_id AND location_id = :location_id"),
            {"user_id": client_id, "location_id": location_id}
        ).fetchone()

        if existing:
            # Удаляем
            db.execute(
                text("DELETE FROM user_favorites WHERE user_id = :user_id AND location_id = :location_id"),
                {"user_id": client_id, "location_id": location_id}
            )
            db.commit()
            logger.info(f"User {client_id} toggled OFF favorite for location {location_id}")
            return {"success": True, "is_favorite": False, "action": "removed"}
        else:
            # Добавляем
            db.execute(
                text("""
                    INSERT INTO user_favorites (user_id, location_id, created_at, updated_at)
                    VALUES (:user_id, :location_id, NOW(), NOW())
                """),
                {"user_id": client_id, "location_id": location_id}
            )
            db.commit()
            logger.info(f"User {client_id} toggled ON favorite for location {location_id}")
            return {"success": True, "is_favorite": True, "action": "added"}

    except Exception as e:
        db.rollback()
        logger.error(f"Error toggling favorite for user {client_id}: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "internal_error", "message": "Failed to toggle favorite"}
        )
