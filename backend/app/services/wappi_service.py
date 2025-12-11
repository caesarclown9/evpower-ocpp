"""
Wappi.pro WhatsApp API Service
Сервис для отправки OTP кодов через WhatsApp
"""
import logging
from typing import Optional
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class WappiService:
    """Клиент для Wappi.pro WhatsApp API"""

    def __init__(self):
        self.api_url = settings.WAPPI_API_URL
        self.api_token = settings.WAPPI_API_TOKEN
        self.profile_id = settings.WAPPI_PROFILE_ID

    @property
    def is_configured(self) -> bool:
        """Проверка, настроен ли сервис"""
        return bool(self.api_token and self.profile_id)

    async def send_message(self, phone: str, message: str) -> bool:
        """
        Отправить сообщение через WhatsApp

        Args:
            phone: Номер телефона в международном формате (+996XXXXXXXXX)
            message: Текст сообщения

        Returns:
            True если сообщение отправлено успешно
        """
        if not self.is_configured:
            logger.warning("[Wappi] Сервис не настроен (WAPPI_API_TOKEN или WAPPI_PROFILE_ID не заданы)")
            # В dev режиме просто логируем OTP
            if not settings.is_production:
                logger.info(f"[Wappi] DEV MODE - сообщение для {phone}: {message}")
                return True
            return False

        # Нормализация номера телефона (убираем +)
        recipient = phone.lstrip("+")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.api_url,
                    params={"profile_id": self.profile_id},
                    headers={
                        "Authorization": self.api_token,
                        "Content-Type": "application/json",
                    },
                    json={
                        "recipient": recipient,
                        "body": message,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    if data.get("status") == "success" or data.get("message_id"):
                        logger.info(f"[Wappi] Сообщение отправлено на {phone}")
                        return True
                    else:
                        logger.error(f"[Wappi] Ошибка API: {data}")
                        return False
                else:
                    logger.error(
                        f"[Wappi] HTTP ошибка {response.status_code}: {response.text}"
                    )
                    return False

        except httpx.TimeoutException:
            logger.error(f"[Wappi] Таймаут при отправке на {phone}")
            return False
        except Exception as e:
            logger.exception(f"[Wappi] Ошибка при отправке: {e}")
            return False

    async def send_otp(self, phone: str, code: str) -> bool:
        """
        Отправить OTP код через WhatsApp

        Args:
            phone: Номер телефона
            code: 6-значный OTP код

        Returns:
            True если код отправлен успешно
        """
        message = (
            f"*EvPower*\n\n"
            f"Ваш код для входа:\n"
            f"```{code}```\n\n"
            f"Код действителен 5 минут.\n"
            f"Если вы не запрашивали код, проигнорируйте это сообщение."
        )
        return await self.send_message(phone, message)


# Singleton instance
wappi_service = WappiService()
