"""
Admin Users API - управление пользователями (только для superadmin)

Позволяет superadmin создавать, редактировать и удалять owners (operators/admins).
"""
import logging
from datetime import datetime
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ....db.session import get_db
from ....core.config import settings

import httpx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["admin-users"])

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

OwnerRole = Literal["operator", "admin", "superadmin"]


class UserCreate(BaseModel):
    """Создание нового owner пользователя"""
    email: EmailStr
    password: str = Field(..., min_length=8, description="Минимум 8 символов")
    role: OwnerRole = "operator"
    name: Optional[str] = None


class UserUpdate(BaseModel):
    """Обновление owner пользователя"""
    role: Optional[OwnerRole] = None
    is_active: Optional[bool] = None
    name: Optional[str] = None


class UserResponse(BaseModel):
    """Ответ с данными пользователя"""
    id: str
    email: str
    role: OwnerRole
    is_active: bool
    name: Optional[str] = None
    created_at: Optional[str] = None
    stations_count: int = 0
    locations_count: int = 0


class UsersListResponse(BaseModel):
    """Список пользователей"""
    success: bool = True
    users: list[UserResponse]
    total: int
    page: int
    per_page: int


class UserDetailResponse(BaseModel):
    """Детальный ответ пользователя"""
    success: bool = True
    user: UserResponse


class MessageResponse(BaseModel):
    """Простой ответ с сообщением"""
    success: bool
    message: str


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_current_user_from_request(request: Request, db: Session) -> dict:
    """
    Извлекает текущего пользователя из request.state и проверяет роль в БД.
    """
    # Middleware устанавливает client_id
    user_id = getattr(request.state, "client_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    # Получаем роль из таблицы users
    row = db.execute(
        text("SELECT id, role, is_active FROM users WHERE id = :id"),
        {"id": user_id}
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещён. Пользователь не является owner."
        )

    if not row.is_active:
        raise HTTPException(
            status_code=403,
            detail="Аккаунт деактивирован"
        )

    return {
        "id": str(row.id),
        "role": row.role,
    }


def require_superadmin(request: Request, db: Session) -> dict:
    """
    Проверяет что текущий пользователь — superadmin.
    """
    user = get_current_user_from_request(request, db)

    if user.get("role") != "superadmin":
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещён. Требуется роль superadmin."
        )
    return user


async def get_supabase_admin_client():
    """
    Возвращает httpx клиент для Supabase Admin API.
    """
    if not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_SERVICE_ROLE_KEY не настроен"
        )

    return httpx.AsyncClient(
        base_url=settings.SUPABASE_URL,
        headers={
            "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=UsersListResponse)
async def list_users(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    role: Optional[OwnerRole] = None,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
):
    """
    Получить список всех owner-пользователей.
    Только для superadmin.
    """
    require_superadmin(request, db)

    try:
        # Базовый запрос
        query = """
            SELECT
                u.id,
                u.email,
                u.role,
                u.is_active,
                c.name,
                u.created_at,
                COALESCE(
                    (SELECT COUNT(*) FROM stations s WHERE s.user_id = u.id),
                    0
                ) as stations_count,
                COALESCE(
                    (SELECT COUNT(*) FROM locations l WHERE l.user_id = u.id),
                    0
                ) as locations_count
            FROM users u
            LEFT JOIN clients c ON c.id = u.id
            WHERE 1=1
        """
        params = {}

        # Фильтры
        if role:
            query += " AND u.role = :role"
            params["role"] = role

        if is_active is not None:
            query += " AND u.is_active = :is_active"
            params["is_active"] = is_active

        if search:
            query += " AND (u.email ILIKE :search OR c.name ILIKE :search)"
            params["search"] = f"%{search}%"

        # Подсчёт общего количества
        count_query = f"SELECT COUNT(*) FROM ({query}) as subq"
        total = db.execute(text(count_query), params).scalar() or 0

        # Пагинация
        offset = (page - 1) * per_page
        query += " ORDER BY u.created_at DESC NULLS LAST LIMIT :limit OFFSET :offset"
        params["limit"] = per_page
        params["offset"] = offset

        rows = db.execute(text(query), params).fetchall()

        users = []
        for row in rows:
            users.append(UserResponse(
                id=str(row.id),
                email=row.email,
                role=row.role,
                is_active=row.is_active,
                name=row.name,
                created_at=row.created_at.isoformat() if row.created_at else None,
                stations_count=row.stations_count or 0,
                locations_count=row.locations_count or 0,
            ))

        return UsersListResponse(
            users=users,
            total=total,
            page=page,
            per_page=per_page,
        )

    except Exception as e:
        logger.error(f"Error listing users: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при получении списка пользователей")


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Получить детали конкретного owner-пользователя.
    Только для superadmin.
    """
    require_superadmin(request, db)

    try:
        query = """
            SELECT
                u.id,
                u.email,
                u.role,
                u.is_active,
                c.name,
                u.created_at,
                COALESCE(
                    (SELECT COUNT(*) FROM stations s WHERE s.user_id = u.id),
                    0
                ) as stations_count,
                COALESCE(
                    (SELECT COUNT(*) FROM locations l WHERE l.user_id = u.id),
                    0
                ) as locations_count
            FROM users u
            LEFT JOIN clients c ON c.id = u.id
            WHERE u.id = :user_id
        """
        row = db.execute(text(query), {"user_id": user_id}).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        return UserDetailResponse(
            user=UserResponse(
                id=str(row.id),
                email=row.email,
                role=row.role,
                is_active=row.is_active,
                name=row.name,
                created_at=row.created_at.isoformat() if row.created_at else None,
                stations_count=row.stations_count or 0,
                locations_count=row.locations_count or 0,
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при получении пользователя")


@router.post("", response_model=UserDetailResponse)
async def create_user(
    data: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Создать нового owner-пользователя.
    Только для superadmin.

    Процесс:
    1. Создаём пользователя в Supabase Auth
    2. Supabase trigger автоматически создаёт запись в public.users
    3. Создаём запись в public.clients для гибридного подхода
    """
    require_superadmin(request, db)

    try:
        # Проверяем что email не занят
        existing = db.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": data.email}
        ).fetchone()

        if existing:
            raise HTTPException(
                status_code=400,
                detail="Пользователь с таким email уже существует"
            )

        # Создаём пользователя через Supabase Admin API
        async with await get_supabase_admin_client() as client:
            response = await client.post(
                "/auth/v1/admin/users",
                json={
                    "email": data.email,
                    "password": data.password,
                    "email_confirm": True,  # Сразу подтверждаем email
                    "user_metadata": {
                        "user_type": data.role,  # operator/admin/superadmin
                        "name": data.name,
                    },
                },
            )

            if response.status_code != 200:
                error_detail = response.json().get("message", "Ошибка создания пользователя")
                logger.error(f"Supabase create user error: {response.text}")
                raise HTTPException(status_code=400, detail=error_detail)

            supabase_user = response.json()
            user_id = supabase_user.get("id")

        if not user_id:
            raise HTTPException(status_code=500, detail="Не удалось получить ID пользователя")

        # Supabase trigger должен создать запись в users, но на всякий случай проверяем
        # и создаём вручную если нужно
        existing_user = db.execute(
            text("SELECT id FROM users WHERE id = :id"),
            {"id": user_id}
        ).fetchone()

        if not existing_user:
            # Создаём запись в users вручную
            db.execute(
                text("""
                    INSERT INTO users (id, email, role, is_active, created_at)
                    VALUES (:id, :email, :role, true, NOW())
                """),
                {"id": user_id, "email": data.email, "role": data.role}
            )
        else:
            # Обновляем роль если отличается
            db.execute(
                text("UPDATE users SET role = :role WHERE id = :id"),
                {"id": user_id, "role": data.role}
            )

        # Создаём запись в clients для гибридного подхода
        existing_client = db.execute(
            text("SELECT id FROM clients WHERE id = :id"),
            {"id": user_id}
        ).fetchone()

        if not existing_client:
            db.execute(
                text("""
                    INSERT INTO clients (id, email, name, balance, status, created_at, updated_at)
                    VALUES (:id, :email, :name, 0, 'active', NOW(), NOW())
                """),
                {"id": user_id, "email": data.email, "name": data.name or ""}
            )

        db.commit()

        logger.info(f"Created owner user: {data.email} with role {data.role}")

        return UserDetailResponse(
            user=UserResponse(
                id=user_id,
                email=data.email,
                role=data.role,
                is_active=True,
                name=data.name,
                created_at=datetime.utcnow().isoformat(),
                stations_count=0,
                locations_count=0,
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating user: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при создании пользователя: {str(e)}")


@router.put("/{user_id}", response_model=UserDetailResponse)
async def update_user(
    user_id: str,
    data: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Обновить owner-пользователя.
    Только для superadmin.
    """
    current_user = require_superadmin(request, db)

    # Нельзя изменять самого себя (защита от случайного понижения)
    if user_id == current_user["id"]:
        raise HTTPException(
            status_code=400,
            detail="Нельзя изменять свой собственный аккаунт"
        )

    try:
        # Проверяем существование
        existing = db.execute(
            text("SELECT id, email, role, is_active FROM users WHERE id = :id"),
            {"id": user_id}
        ).fetchone()

        if not existing:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        # Собираем поля для обновления
        updates = []
        params = {"id": user_id}

        if data.role is not None:
            updates.append("role = :role")
            params["role"] = data.role

        if data.is_active is not None:
            updates.append("is_active = :is_active")
            params["is_active"] = data.is_active

        if updates:
            query = f"UPDATE users SET {', '.join(updates)} WHERE id = :id"
            db.execute(text(query), params)

        # Обновляем имя в clients если передано
        if data.name is not None:
            db.execute(
                text("UPDATE clients SET name = :name, updated_at = NOW() WHERE id = :id"),
                {"id": user_id, "name": data.name}
            )

        db.commit()

        # Возвращаем обновлённые данные
        return await get_user(user_id, request, db)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при обновлении пользователя")


@router.delete("/{user_id}", response_model=MessageResponse)
async def delete_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Удалить (деактивировать) owner-пользователя.
    Только для superadmin.

    Не удаляем физически, а деактивируем (is_active = false).
    """
    current_user = require_superadmin(request, db)

    # Нельзя удалять самого себя
    if user_id == current_user["id"]:
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить свой собственный аккаунт"
        )

    try:
        # Проверяем существование
        existing = db.execute(
            text("SELECT id, email FROM users WHERE id = :id"),
            {"id": user_id}
        ).fetchone()

        if not existing:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        # Деактивируем пользователя
        db.execute(
            text("UPDATE users SET is_active = false WHERE id = :id"),
            {"id": user_id}
        )

        # Также деактивируем в clients
        db.execute(
            text("UPDATE clients SET status = 'inactive', updated_at = NOW() WHERE id = :id"),
            {"id": user_id}
        )

        db.commit()

        logger.info(f"Deactivated user: {existing.email}")

        return MessageResponse(
            success=True,
            message=f"Пользователь {existing.email} деактивирован"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при удалении пользователя")


@router.post("/{user_id}/activate", response_model=MessageResponse)
async def activate_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Активировать деактивированного пользователя.
    Только для superadmin.
    """
    require_superadmin(request, db)

    try:
        existing = db.execute(
            text("SELECT id, email, is_active FROM users WHERE id = :id"),
            {"id": user_id}
        ).fetchone()

        if not existing:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        if existing.is_active:
            return MessageResponse(
                success=True,
                message="Пользователь уже активен"
            )

        db.execute(
            text("UPDATE users SET is_active = true WHERE id = :id"),
            {"id": user_id}
        )

        db.execute(
            text("UPDATE clients SET status = 'active', updated_at = NOW() WHERE id = :id"),
            {"id": user_id}
        )

        db.commit()

        logger.info(f"Activated user: {existing.email}")

        return MessageResponse(
            success=True,
            message=f"Пользователь {existing.email} активирован"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error activating user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при активации пользователя")
