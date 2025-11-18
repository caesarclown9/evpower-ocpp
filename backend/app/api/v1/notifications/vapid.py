"""
VAPID Public Key API
Endpoint для получения VAPID public key
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings

router = APIRouter()


class VapidPublicKeyResponse(BaseModel):
    """Ответ с VAPID public key"""
    success: bool
    data: dict


@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
async def get_vapid_public_key():
    """
    🔑 Получить VAPID public key

    Возвращает VAPID public key для подписки на push notifications.

    **Аутентификация НЕ требуется** - public key публичный по определению.

    **Возвращает:**
    - public_key: VAPID public key (base64url)

    **Пример использования в PWA:**
    ```javascript
    const response = await fetch('/api/v1/notifications/vapid-public-key');
    const { data } = await response.json();
    const vapidPublicKey = data.public_key;

    // Использовать для подписки
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: vapidPublicKey
    });
    ```
    """
    return VapidPublicKeyResponse(
        success=True,
        data={
            "public_key": settings.VAPID_PUBLIC_KEY
        }
    )
