"""
Push Notifications Subscriptions API
Endpoints для управления push subscriptions
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel, Field
from typing import Optional
import logging

from app.db.session import get_db
from app.services.push_service import push_service

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================================
# PYDANTIC SCHEMAS
# ============================================================================

class PushSubscriptionKeys(BaseModel):
    """Ключи шифрования от браузера"""
    p256dh: str = Field(..., description="P256DH public key (base64)")
    auth: str = Field(..., description="Auth secret (base64)")


class PushSubscriptionData(BaseModel):
    """PushSubscription объект от браузера"""
    endpoint: str = Field(..., description="Push service endpoint URL")
    keys: PushSubscriptionKeys


class SubscribeRequest(BaseModel):
    """Запрос на подписку на push notifications"""
    subscription: PushSubscriptionData
    user_type: str = Field(..., regex="^(client|owner)$", description="Тип пользователя: client или owner")


class UnsubscribeRequest(BaseModel):
    """Запрос на отписку от push notifications"""
    endpoint: str = Field(..., description="Endpoint для удаления")


class SubscriptionResponse(BaseModel):
    """Ответ на операцию с подпиской"""
    success: bool
    message: str
    subscription_id: Optional[str] = None


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/subscribe", response_model=SubscriptionResponse)
async def subscribe_to_push(
    request: SubscribeRequest,
    db: Session = Depends(get_db),
    http_request: Request = None
):
    """
    📱 Подписаться на push notifications

    Регистрирует PushSubscription от браузера для получения уведомлений.

    **Требуется аутентификация:** JWT token в Authorization header

    **Параметры:**
    - subscription: PushSubscription объект от браузера
    - user_type: "client" или "owner"

    **Возвращает:**
    - success: true если подписка создана
    - subscription_id: UUID подписки
    - message: Описание результата
    """
    # Получаем user_id из JWT (через AuthMiddleware)
    user_id = getattr(http_request.state, "client_id", None)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid authentication"
        )

    try:
        # Проверяем существует ли уже подписка с таким endpoint
        existing = db.execute(text("""
            SELECT id FROM push_subscriptions
            WHERE user_id = :user_id AND endpoint = :endpoint
        """), {
            "user_id": user_id,
            "endpoint": request.subscription.endpoint
        }).fetchone()

        if existing:
            # Обновляем существующую подписку (keys могли измениться)
            db.execute(text("""
                UPDATE push_subscriptions
                SET p256dh_key = :p256dh_key,
                    auth_key = :auth_key,
                    user_type = :user_type,
                    updated_at = NOW()
                WHERE id = :sub_id
            """), {
                "sub_id": existing.id,
                "p256dh_key": request.subscription.keys.p256dh,
                "auth_key": request.subscription.keys.auth,
                "user_type": request.user_type
            })

            db.commit()

            logger.info(f"Updated push subscription for user {user_id}, endpoint: {request.subscription.endpoint[:50]}...")

            return SubscriptionResponse(
                success=True,
                message="Push subscription updated successfully",
                subscription_id=str(existing.id)
            )

        # Создаём новую подписку
        result = db.execute(text("""
            INSERT INTO push_subscriptions
            (user_id, user_type, endpoint, p256dh_key, auth_key, user_agent)
            VALUES (:user_id, :user_type, :endpoint, :p256dh_key, :auth_key, :user_agent)
            RETURNING id
        """), {
            "user_id": user_id,
            "user_type": request.user_type,
            "endpoint": request.subscription.endpoint,
            "p256dh_key": request.subscription.keys.p256dh,
            "auth_key": request.subscription.keys.auth,
            "user_agent": http_request.headers.get("user-agent", None)
        }).fetchone()

        db.commit()

        logger.info(f"Created push subscription for user {user_id}, type: {request.user_type}, endpoint: {request.subscription.endpoint[:50]}...")

        return SubscriptionResponse(
            success=True,
            message="Push subscription registered successfully",
            subscription_id=str(result.id)
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Error subscribing to push notifications: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register push subscription: {str(e)}"
        )


@router.post("/unsubscribe", response_model=SubscriptionResponse)
async def unsubscribe_from_push(
    request: UnsubscribeRequest,
    db: Session = Depends(get_db),
    http_request: Request = None
):
    """
    🔕 Отписаться от push notifications

    Удаляет PushSubscription для указанного endpoint.

    **Требуется аутентификация:** JWT token в Authorization header

    **Параметры:**
    - endpoint: Endpoint для удаления

    **Возвращает:**
    - success: true если подписка удалена
    - message: Описание результата
    """
    # Получаем user_id из JWT
    user_id = getattr(http_request.state, "client_id", None)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid authentication"
        )

    try:
        # Удаляем подписку
        result = db.execute(text("""
            DELETE FROM push_subscriptions
            WHERE user_id = :user_id AND endpoint = :endpoint
            RETURNING id
        """), {
            "user_id": user_id,
            "endpoint": request.endpoint
        }).fetchone()

        db.commit()

        if not result:
            logger.warning(f"Attempted to unsubscribe from non-existent push subscription: {request.endpoint[:50]}...")
            return SubscriptionResponse(
                success=False,
                message="Push subscription not found"
            )

        logger.info(f"Removed push subscription for user {user_id}, endpoint: {request.endpoint[:50]}...")

        return SubscriptionResponse(
            success=True,
            message="Push subscription removed successfully"
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Error unsubscribing from push notifications: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove push subscription: {str(e)}"
        )


@router.post("/test", response_model=dict)
async def test_push_notification(
    db: Session = Depends(get_db),
    http_request: Request = None
):
    """
    🧪 Отправить тестовое push notification

    Отправляет тестовое уведомление на все subscriptions текущего пользователя.

    **Требуется аутентификация:** JWT token в Authorization header

    **Возвращает:**
    - success: true если хотя бы одно уведомление отправлено
    - sent_to: количество успешно отправленных уведомлений
    - message: Описание результата
    """
    # Получаем user_id из JWT
    user_id = getattr(http_request.state, "client_id", None)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid authentication"
        )

    try:
        # Определяем user_type (проверяем в clients или users)
        client_check = db.execute(text("""
            SELECT id FROM clients WHERE id = :user_id
        """), {"user_id": user_id}).fetchone()

        user_type = "client" if client_check else "owner"

        # Отправляем тестовое уведомление
        result = await push_service.send_notification(
            db=db,
            user_id=user_id,
            user_type=user_type,
            title="Test Notification",
            body="This is a test push notification from EvPower",
            icon="/logo-192.png",
            data={
                "type": "test",
                "timestamp": str(db.execute(text("SELECT NOW()")).scalar())
            }
        )

        if result.get("success"):
            return {
                "success": True,
                "sent_to": result.get("sent_count", 0),
                "message": f"Test notification sent to {result.get('sent_count', 0)} device(s)"
            }
        else:
            return {
                "success": False,
                "sent_to": 0,
                "message": result.get("reason", "Failed to send test notification")
            }

    except Exception as e:
        logger.error(f"Error sending test push notification: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to send test notification: {str(e)}"
        )
