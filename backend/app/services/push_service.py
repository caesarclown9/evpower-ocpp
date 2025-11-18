"""
Сервис для отправки Web Push Notifications
"""
from typing import Dict, Any, Optional, List
import json
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text
from pywebpush import webpush, WebPushException

from app.core.config import settings
from app.db.session import get_db

logger = logging.getLogger(__name__)


class PushNotificationService:
    """
    Сервис для отправки Web Push Notifications

    Использует Web Push API (RFC 8030) с VAPID (RFC 8292)
    """

    def __init__(self):
        self.enabled = settings.PUSH_NOTIFICATIONS_ENABLED
        self.vapid_private_key = settings.VAPID_PRIVATE_KEY
        self.vapid_claims = {
            "sub": settings.VAPID_SUBJECT
        }
        self.ttl = settings.PUSH_TTL
        self.max_retries = settings.PUSH_MAX_RETRIES

    async def send_notification(
        self,
        db: Session,
        user_id: str,
        user_type: str,
        title: str,
        body: str,
        icon: str = "/logo-192.png",
        badge: str = "/logo-96.png",
        data: dict = None,
        actions: list = None,
        tag: str = None,
        require_interaction: bool = False
    ) -> Dict[str, Any]:
        """
        Отправить push notification всем subscriptions пользователя

        Args:
            db: Database session
            user_id: UUID пользователя (из auth.users)
            user_type: Тип пользователя ('client' или 'owner')
            title: Заголовок уведомления
            body: Текст уведомления
            icon: Путь к иконке (относительный от domain)
            badge: Путь к badge иконке
            data: Дополнительные данные (dict)
            actions: Действия для уведомления (list)
            tag: Тег для группировки уведомлений
            require_interaction: Требовать взаимодействие пользователя

        Returns:
            dict: {
                "success": bool,
                "sent_count": int,
                "failed_count": int,
                "details": []
            }
        """
        if not self.enabled:
            logger.info("Push notifications disabled, skipping")
            return {
                "success": False,
                "sent_count": 0,
                "failed_count": 0,
                "reason": "Push notifications disabled"
            }

        if not self.vapid_private_key:
            logger.warning("VAPID private key not configured, skipping push")
            return {
                "success": False,
                "sent_count": 0,
                "failed_count": 0,
                "reason": "VAPID keys not configured"
            }

        # Получить все subscriptions пользователя
        try:
            subscriptions = db.execute(text("""
                SELECT id, endpoint, p256dh_key, auth_key
                FROM push_subscriptions
                WHERE user_id = :user_id AND user_type = :user_type
            """), {"user_id": user_id, "user_type": user_type}).fetchall()

            if not subscriptions:
                logger.info(f"No push subscriptions found for user {user_id} (type: {user_type})")
                return {
                    "success": False,
                    "sent_count": 0,
                    "failed_count": 0,
                    "reason": "No subscriptions found"
                }

            # Подготовить payload
            notification_payload = {
                "title": title,
                "body": body,
                "icon": icon,
                "badge": badge,
                "data": data or {},
            }

            if actions:
                notification_payload["actions"] = actions

            if tag:
                notification_payload["tag"] = tag

            if require_interaction:
                notification_payload["requireInteraction"] = True

            sent_count = 0
            failed_count = 0
            invalid_subscriptions = []

            # Отправить на все subscriptions
            for sub in subscriptions:
                try:
                    subscription_info = {
                        "endpoint": sub.endpoint,
                        "keys": {
                            "p256dh": sub.p256dh_key,
                            "auth": sub.auth_key
                        }
                    }

                    # Отправить через pywebpush
                    webpush(
                        subscription_info=subscription_info,
                        data=json.dumps(notification_payload),
                        vapid_private_key=self.vapid_private_key,
                        vapid_claims=self.vapid_claims,
                        ttl=self.ttl
                    )

                    sent_count += 1

                    # Обновить last_used_at
                    db.execute(text("""
                        UPDATE push_subscriptions
                        SET last_used_at = NOW()
                        WHERE id = :sub_id
                    """), {"sub_id": sub.id})

                    logger.info(f"Push sent successfully to {sub.endpoint[:50]}...")

                except WebPushException as e:
                    logger.error(f"Failed to send push to {sub.endpoint[:50]}...: {e}")
                    failed_count += 1

                    # Если subscription недействительна (410 Gone или 404) - удалить
                    if hasattr(e, 'response') and e.response and e.response.status_code in [404, 410]:
                        invalid_subscriptions.append(sub.id)
                        logger.info(f"Marking subscription {sub.id} for removal (invalid endpoint)")

                except Exception as e:
                    logger.error(f"Unexpected error sending push to {sub.endpoint[:50]}...: {e}")
                    failed_count += 1

            # Удалить недействительные subscriptions
            if invalid_subscriptions:
                db.execute(text("""
                    DELETE FROM push_subscriptions
                    WHERE id = ANY(:ids)
                """), {"ids": invalid_subscriptions})
                logger.info(f"Removed {len(invalid_subscriptions)} invalid push subscriptions")

            db.commit()

            logger.info(f"Push sent to user {user_id}: {sent_count} success, {failed_count} failed")

            return {
                "success": sent_count > 0,
                "sent_count": sent_count,
                "failed_count": failed_count,
                "removed_invalid": len(invalid_subscriptions)
            }

        except Exception as e:
            logger.error(f"Error sending push notifications to user {user_id}: {e}", exc_info=True)
            db.rollback()
            return {
                "success": False,
                "sent_count": 0,
                "failed_count": 0,
                "error": str(e)
            }

    async def send_to_client(
        self,
        db: Session,
        client_id: str,
        event_type: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Отправить push notification клиенту (wrapper для удобства)

        Args:
            db: Database session
            client_id: UUID клиента
            event_type: Тип события ('charging_started', 'charging_completed', etc.)
            **kwargs: Дополнительные параметры (title, body, data, etc.)

        Returns:
            dict: Результат отправки
        """
        try:
            # Определяем title и body в зависимости от типа события
            notification_config = self._get_client_notification_config(event_type, kwargs)

            return await self.send_notification(
                db=db,
                user_id=client_id,
                user_type="client",
                **notification_config
            )

        except Exception as e:
            logger.error(f"Error sending client push (event: {event_type}): {e}")
            return {"success": False, "error": str(e)}

    async def send_to_owner(
        self,
        db: Session,
        owner_id: str,
        event_type: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Отправить push notification владельцу станции (wrapper для удобства)

        Args:
            db: Database session
            owner_id: UUID владельца (из users таблицы)
            event_type: Тип события ('new_session', 'session_completed', etc.)
            **kwargs: Дополнительные параметры

        Returns:
            dict: Результат отправки
        """
        try:
            # Определяем title и body в зависимости от типа события
            notification_config = self._get_owner_notification_config(event_type, kwargs)

            return await self.send_notification(
                db=db,
                user_id=owner_id,
                user_type="owner",
                **notification_config
            )

        except Exception as e:
            logger.error(f"Error sending owner push (event: {event_type}): {e}")
            return {"success": False, "error": str(e)}

    def _get_client_notification_config(self, event_type: str, params: dict) -> dict:
        """Получить конфигурацию уведомления для клиента"""

        configs = {
            "charging_started": {
                "title": "Зарядка началась",
                "body": f"Станция {params.get('station_id', 'N/A')}, коннектор {params.get('connector_id', 'N/A')}",
                "icon": "/icons/charging-start.png",
                "data": {
                    "type": "charging_started",
                    "session_id": params.get("session_id"),
                    "station_id": params.get("station_id"),
                    "connector_id": params.get("connector_id")
                },
                "actions": [
                    {"action": "view", "title": "Открыть"}
                ]
            },
            "charging_completed": {
                "title": "Зарядка завершена",
                "body": f"{params.get('energy_kwh', 0):.2f} кВт⋅ч за {params.get('amount', 0):.2f} сом",
                "icon": "/icons/charging-complete.png",
                "data": {
                    "type": "charging_completed",
                    "session_id": params.get("session_id"),
                    "energy_kwh": params.get("energy_kwh"),
                    "amount": params.get("amount")
                },
                "actions": [
                    {"action": "view_history", "title": "Посмотреть"}
                ]
            },
            "charging_error": {
                "title": "Ошибка зарядки",
                "body": params.get("error_message", "Произошла ошибка при зарядке"),
                "icon": "/icons/charging-error.png",
                "data": {
                    "type": "charging_error",
                    "session_id": params.get("session_id"),
                    "error_code": params.get("error_code")
                },
                "require_interaction": True
            },
            "low_balance": {
                "title": "Низкий баланс",
                "body": f"Ваш баланс: {params.get('balance', 0):.2f} сом. Пополните для зарядки.",
                "icon": "/icons/low-balance.png",
                "data": {
                    "type": "low_balance",
                    "balance": params.get("balance")
                },
                "actions": [
                    {"action": "topup", "title": "Пополнить"}
                ]
            },
            "payment_confirmed": {
                "title": "Баланс пополнен",
                "body": f"Зачислено {params.get('amount', 0):.2f} сом",
                "icon": "/icons/payment-success.png",
                "data": {
                    "type": "payment_confirmed",
                    "amount": params.get("amount"),
                    "new_balance": params.get("new_balance")
                }
            }
        }

        return configs.get(event_type, {
            "title": params.get("title", "Уведомление"),
            "body": params.get("body", ""),
            "icon": "/logo-192.png",
            "data": params.get("data", {})
        })

    def _get_owner_notification_config(self, event_type: str, params: dict) -> dict:
        """Получить конфигурацию уведомления для владельца"""

        station_name = params.get("station_name", params.get("station_id", "N/A"))

        configs = {
            "new_session": {
                "title": "Новая зарядка",
                "body": f"Станция {station_name}, коннектор {params.get('connector_id', 'N/A')}",
                "icon": "/icons/session-new.png",
                "data": {
                    "type": "new_session",
                    "session_id": params.get("session_id"),
                    "station_id": params.get("station_id"),
                    "location_id": params.get("location_id")
                },
                "actions": [
                    {"action": "view_station", "title": "Открыть станцию"}
                ]
            },
            "session_completed": {
                "title": "Зарядка завершена",
                "body": f"{params.get('energy_kwh', 0):.2f} кВт⋅ч, доход {params.get('amount', 0):.2f} сом",
                "icon": "/icons/session-complete.png",
                "data": {
                    "type": "session_completed",
                    "session_id": params.get("session_id"),
                    "energy_kwh": params.get("energy_kwh"),
                    "amount": params.get("amount")
                }
            },
            "station_offline": {
                "title": "Станция оффлайн",
                "body": f"{station_name} не отвечает",
                "icon": "/icons/station-offline.png",
                "data": {
                    "type": "station_offline",
                    "station_id": params.get("station_id"),
                    "offline_since": params.get("offline_since")
                },
                "actions": [
                    {"action": "view_station", "title": "Проверить"}
                ],
                "require_interaction": True
            }
        }

        return configs.get(event_type, {
            "title": params.get("title", "Уведомление"),
            "body": params.get("body", ""),
            "icon": "/logo-192.png",
            "data": params.get("data", {})
        })


# Singleton instance
push_service = PushNotificationService()


def get_station_owner_id(db: Session, station_id: str) -> Optional[str]:
    """
    Получить owner_id станции через location

    Args:
        db: Database session
        station_id: ID станции

    Returns:
        str: owner_id (user_id из locations таблицы) или None
    """
    try:
        result = db.execute(text("""
            SELECT l.user_id
            FROM stations s
            JOIN locations l ON s.location_id = l.id
            WHERE s.id = :station_id
        """), {"station_id": station_id}).fetchone()

        return result[0] if result else None

    except Exception as e:
        logger.error(f"Error getting station owner_id for {station_id}: {e}")
        return None
