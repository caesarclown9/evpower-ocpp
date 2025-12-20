"""
Operators API - управление операторами (для admin)

Admin может создавать операторов по номеру телефона.
Операторы получают доступ ко всем станциям своего admin.
"""
import logging
from uuid import uuid4
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ....db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/operators", tags=["admin-operators"])


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────────────────────

class OperatorCreate(BaseModel):
    """Создание оператора по номеру телефона"""
    phone: str = Field(..., min_length=10, max_length=20)
    name: Optional[str] = None


class OperatorResponse(BaseModel):
    """Ответ с данными оператора"""
    id: str
    phone: str
    name: Optional[str] = None
    is_active: bool = True


class OperatorsListResponse(BaseModel):
    """Список операторов"""
    success: bool = True
    operators: list[OperatorResponse]


class MessageResponse(BaseModel):
    """Простой ответ с сообщением"""
    success: bool
    message: str


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    """Нормализация телефона к формату +996..."""
    phone = "".join(c for c in phone if c.isdigit() or c == "+")
    if not phone.startswith("+"):
        phone = "+" + phone
    return phone


def get_current_admin(request: Request, db: Session) -> dict:
    """
    Получить текущего admin/superadmin.
    Операторы не могут управлять другими операторами.
    """
    user_id = getattr(request.state, "client_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Не авторизован")

    row = db.execute(
        text("SELECT id, role, is_active FROM users WHERE id = :id"),
        {"id": user_id}
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещён. Пользователь не является owner."
        )

    if row.role not in ("admin", "superadmin"):
        raise HTTPException(
            status_code=403,
            detail="Требуется роль admin или superadmin"
        )

    if not row.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт деактивирован")

    return {"id": str(row.id), "role": row.role}


# ─────────────────────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("", response_model=OperatorsListResponse)
async def list_operators(request: Request, db: Session = Depends(get_db)):
    """
    Список операторов текущего admin.
    Superadmin видит всех операторов.
    """
    admin = get_current_admin(request, db)

    if admin["role"] == "superadmin":
        # Superadmin видит всех операторов
        rows = db.execute(
            text("""
                SELECT u.id, u.phone, c.name, u.is_active
                FROM users u
                LEFT JOIN clients c ON c.id = u.id
                WHERE u.role = 'operator'
                ORDER BY u.created_at DESC
            """)
        ).fetchall()
    else:
        # Admin видит только своих операторов
        rows = db.execute(
            text("""
                SELECT u.id, u.phone, c.name, u.is_active
                FROM users u
                LEFT JOIN clients c ON c.id = u.id
                WHERE u.admin_id = :admin_id AND u.role = 'operator'
                ORDER BY u.created_at DESC
            """),
            {"admin_id": admin["id"]}
        ).fetchall()

    operators = [
        OperatorResponse(
            id=str(r.id),
            phone=r.phone or "",
            name=r.name,
            is_active=r.is_active
        )
        for r in rows
    ]

    return OperatorsListResponse(operators=operators)


@router.post("", response_model=OperatorResponse)
async def create_operator(
    data: OperatorCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Создать оператора по номеру телефона.
    Оператор сможет войти через WhatsApp OTP.
    """
    admin = get_current_admin(request, db)
    phone = normalize_phone(data.phone)

    try:
        # Проверяем что телефон не занят в users
        existing = db.execute(
            text("SELECT id FROM users WHERE phone = :phone"),
            {"phone": phone}
        ).fetchone()

        if existing:
            raise HTTPException(
                status_code=400,
                detail="Этот номер уже зарегистрирован как владелец"
            )

        # Также проверяем в clients
        existing_client = db.execute(
            text("SELECT id FROM clients WHERE phone = :phone"),
            {"phone": phone}
        ).fetchone()

        if existing_client:
            raise HTTPException(
                status_code=400,
                detail="Этот номер уже используется клиентом"
            )

        # Создаём оператора
        operator_id = str(uuid4())

        db.execute(
            text("""
                INSERT INTO users (id, phone, role, admin_id, is_active, created_at, updated_at)
                VALUES (:id, :phone, 'operator', :admin_id, true, NOW(), NOW())
            """),
            {"id": operator_id, "phone": phone, "admin_id": admin["id"]}
        )

        # Создаём client запись для гибридного функционала
        db.execute(
            text("""
                INSERT INTO clients (id, phone, name, balance, status, created_at, updated_at)
                VALUES (:id, :phone, :name, 0, 'active', NOW(), NOW())
            """),
            {"id": operator_id, "phone": phone, "name": data.name or ""}
        )

        db.commit()

        logger.info(f"Created operator {phone} for admin {admin['id']}")

        return OperatorResponse(
            id=operator_id,
            phone=phone,
            name=data.name,
            is_active=True
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating operator: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при создании оператора")


@router.delete("/{operator_id}", response_model=MessageResponse)
async def deactivate_operator(
    operator_id: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Деактивировать оператора.
    Admin может деактивировать только своих операторов.
    Superadmin может деактивировать любого оператора.
    """
    admin = get_current_admin(request, db)

    try:
        # Проверяем что оператор существует и принадлежит этому admin
        row = db.execute(
            text("SELECT id, phone, admin_id FROM users WHERE id = :id AND role = 'operator'"),
            {"id": operator_id}
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Оператор не найден")

        # Проверяем права доступа
        if admin["role"] != "superadmin" and str(row.admin_id) != admin["id"]:
            raise HTTPException(
                status_code=403,
                detail="Нет доступа к этому оператору"
            )

        # Деактивируем в users
        db.execute(
            text("UPDATE users SET is_active = false WHERE id = :id"),
            {"id": operator_id}
        )

        # Деактивируем в clients
        db.execute(
            text("UPDATE clients SET status = 'inactive', updated_at = NOW() WHERE id = :id"),
            {"id": operator_id}
        )

        db.commit()

        logger.info(f"Deactivated operator {row.phone}")

        return MessageResponse(
            success=True,
            message="Оператор деактивирован"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deactivating operator {operator_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при деактивации оператора")


@router.post("/{operator_id}/activate", response_model=MessageResponse)
async def activate_operator(
    operator_id: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Активировать деактивированного оператора.
    """
    admin = get_current_admin(request, db)

    try:
        row = db.execute(
            text("SELECT id, phone, admin_id, is_active FROM users WHERE id = :id AND role = 'operator'"),
            {"id": operator_id}
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Оператор не найден")

        # Проверяем права доступа
        if admin["role"] != "superadmin" and str(row.admin_id) != admin["id"]:
            raise HTTPException(
                status_code=403,
                detail="Нет доступа к этому оператору"
            )

        if row.is_active:
            return MessageResponse(success=True, message="Оператор уже активен")

        db.execute(
            text("UPDATE users SET is_active = true WHERE id = :id"),
            {"id": operator_id}
        )

        db.execute(
            text("UPDATE clients SET status = 'active', updated_at = NOW() WHERE id = :id"),
            {"id": operator_id}
        )

        db.commit()

        logger.info(f"Activated operator {row.phone}")

        return MessageResponse(
            success=True,
            message="Оператор активирован"
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error activating operator {operator_id}: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при активации оператора")
