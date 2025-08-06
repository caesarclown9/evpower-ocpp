"""
Безопасное логирование с маскированием чувствительных данных
"""
import re
import logging
from typing import Any, Dict, Union
import json

class SecureFormatter(logging.Formatter):
    """Форматтер для маскирования чувствительных данных в логах"""
    
    # Паттерны для чувствительных данных
    PATTERNS = {
        # Номера карт (13-19 цифр)
        'card_number': re.compile(r'\b(?:\d{4}[\s\-]?){3,4}\d{1,4}\b'),
        # CVV (3-4 цифры)
        'cvv': re.compile(r'\b(cvv|cvc|cvv2|cvc2)[\s:=]*\d{3,4}\b', re.IGNORECASE),
        # Email адреса (маскируем часть)
        'email': re.compile(r'\b([a-zA-Z0-9._%+-]+)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b'),
        # Токены (выглядят как длинные случайные строки)
        'token': re.compile(r'\b(token|bearer|api_key|apikey)[\s:=]*[a-zA-Z0-9\-_]{20,}\b', re.IGNORECASE),
        # Пароли
        'password': re.compile(r'\b(password|pwd|pass)[\s:=]*[^\s]+\b', re.IGNORECASE),
    }
    
    def format(self, record: logging.LogRecord) -> str:
        """Форматирует лог запись с маскированием"""
        # Получаем оригинальное сообщение
        msg = super().format(record)
        
        # Маскируем чувствительные данные
        msg = self._mask_sensitive_data(msg)
        
        return msg
    
    def _mask_sensitive_data(self, text: str) -> str:
        """Маскирует чувствительные данные в тексте"""
        # Маскируем номера карт
        text = self.PATTERNS['card_number'].sub(self._mask_card_number, text)
        
        # Маскируем CVV
        text = self.PATTERNS['cvv'].sub(lambda m: m.group(1) + ': ***', text)
        
        # Маскируем email (показываем только первые 2 символа и домен)
        text = self.PATTERNS['email'].sub(self._mask_email, text)
        
        # Маскируем токены
        text = self.PATTERNS['token'].sub(self._mask_token, text)
        
        # Маскируем пароли
        text = self.PATTERNS['password'].sub(lambda m: m.group(1) + ': ****', text)
        
        return text
    
    def _mask_card_number(self, match: re.Match) -> str:
        """Маскирует номер карты, оставляя последние 4 цифры"""
        card = match.group(0).replace(' ', '').replace('-', '')
        if len(card) >= 8:
            return f"****{card[-4:]}"
        return "****"
    
    def _mask_email(self, match: re.Match) -> str:
        """Маскирует email, показывая только первые 2 символа"""
        local, domain = match.groups()
        if len(local) > 2:
            return f"{local[:2]}***@{domain}"
        return f"***@{domain}"
    
    def _mask_token(self, match: re.Match) -> str:
        """Маскирует токен, показывая только префикс"""
        parts = match.group(0).split(':', 1)
        if len(parts) == 2:
            token_value = parts[1].strip()
            if len(token_value) > 8:
                return f"{parts[0]}: {token_value[:4]}...{token_value[-4:]}"
        return match.group(0).split()[0] + ": ****"

def sanitize_dict(data: Dict[str, Any], sensitive_keys: set = None) -> Dict[str, Any]:
    """
    Рекурсивно маскирует чувствительные данные в словаре
    
    Args:
        data: Словарь для обработки
        sensitive_keys: Множество ключей для маскирования
        
    Returns:
        Словарь с замаскированными значениями
    """
    if sensitive_keys is None:
        sensitive_keys = {
            'password', 'pwd', 'pass', 'secret', 'token', 'api_key', 'apikey',
            'card_number', 'card_pan', 'cvv', 'cvc', 'cvv2', 'cvc2',
            'authorization', 'auth', 'bearer'
        }
    
    sanitized = {}
    
    for key, value in data.items():
        lower_key = key.lower()
        
        # Проверяем, является ли ключ чувствительным
        if any(sensitive in lower_key for sensitive in sensitive_keys):
            if isinstance(value, str):
                # Маскируем строковые значения
                if len(value) > 4:
                    sanitized[key] = f"{value[:2]}...{value[-2:]}"
                else:
                    sanitized[key] = "****"
            else:
                sanitized[key] = "****"
        elif isinstance(value, dict):
            # Рекурсивно обрабатываем вложенные словари
            sanitized[key] = sanitize_dict(value, sensitive_keys)
        elif isinstance(value, list):
            # Обрабатываем списки
            sanitized[key] = [
                sanitize_dict(item, sensitive_keys) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            # Оставляем остальные значения как есть
            sanitized[key] = value
    
    return sanitized

def log_payment_operation(
    logger: logging.Logger,
    operation: str,
    client_id: str,
    amount: float,
    extra_data: Dict[str, Any] = None,
    success: bool = True
):
    """
    Безопасное логирование платежных операций
    
    Args:
        logger: Logger instance
        operation: Тип операции (topup, payment, refund)
        client_id: ID клиента
        amount: Сумма операции
        extra_data: Дополнительные данные
        success: Успешность операции
    """
    log_data = {
        "event": f"payment_{operation}",
        "client_id": client_id,
        "amount": amount,
        "currency": "KGS",
        "success": success
    }
    
    if extra_data:
        # Очищаем чувствительные данные
        log_data.update(sanitize_dict(extra_data))
    
    level = logging.INFO if success else logging.ERROR
    logger.log(level, f"Payment {operation}: {client_id}", extra=log_data)

# Функция для настройки безопасного логирования
def setup_secure_logging():
    """Настраивает безопасное логирование для всего приложения"""
    # Создаем безопасный форматтер
    secure_formatter = SecureFormatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Применяем ко всем хендлерам
    for handler in logging.root.handlers:
        handler.setFormatter(secure_formatter)
    
    # Также применяем к логгерам приложения
    app_loggers = [
        'app', 'ocpp', 'uvicorn', 'fastapi'
    ]
    
    for logger_name in app_loggers:
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers:
            handler.setFormatter(secure_formatter)