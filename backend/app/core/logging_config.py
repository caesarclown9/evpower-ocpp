import logging
import logging.config
import json
import uuid
from datetime import datetime
from typing import Any, Dict
import sys
from contextvars import ContextVar

# Context variable для correlation ID
correlation_id: ContextVar[str] = ContextVar('correlation_id', default='')

class StructuredFormatter(logging.Formatter):
    """Форматтер для структурированных JSON логов"""
    
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id.get(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Добавляем exception info если есть
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # Добавляем дополнительные поля из record
        for key, value in record.__dict__.items():
            if key not in ('name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 
                          'filename', 'module', 'exc_info', 'exc_text', 'stack_info',
                          'lineno', 'funcName', 'created', 'msecs', 'relativeCreated',
                          'thread', 'threadName', 'processName', 'process', 'getMessage'):
                log_entry[key] = value
        
        return json.dumps(log_entry, ensure_ascii=False)

def setup_logging():
    """Настройка системы логирования"""
    
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": StructuredFormatter,
            },
            "simple": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "structured",
                "stream": sys.stdout
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "ERROR",
                "formatter": "structured",
                "filename": "/tmp/app_errors.log",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5
            }
        },
        "loggers": {
            "": {  # Root logger
                "level": "INFO",
                "handlers": ["console", "error_file"]
            },
            "uvicorn": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False
            },
            "uvicorn.error": {
                "level": "INFO", 
                "handlers": ["console"],
                "propagate": False
            },
            "uvicorn.access": {
                "level": "INFO",
                "handlers": ["console"], 
                "propagate": False
            }
        }
    }
    
    logging.config.dictConfig(config)

def get_correlation_id() -> str:
    """Получить текущий correlation ID"""
    return correlation_id.get()

def set_correlation_id(cid: str = None) -> str:
    """Установить correlation ID"""
    if cid is None:
        cid = str(uuid.uuid4())
    correlation_id.set(cid)
    return cid

def log_payment_event(event_type: str, amount: float, station_id: str, transaction_id: str = None, **kwargs):
    """Логирование платежных событий"""
    logger = logging.getLogger("payment")
    
    event_data = {
        "event_type": event_type,
        "amount": amount,
        "station_id": station_id,
        "transaction_id": transaction_id,
        **kwargs
    }
    
    logger.info(f"Payment event: {event_type}", extra=event_data)

def log_ocpp_event(event_type: str, station_id: str, message_type: str = None, **kwargs):
    """Логирование OCPP событий"""
    logger = logging.getLogger("ocpp")
    
    event_data = {
        "event_type": event_type,
        "station_id": station_id,
        "message_type": message_type,
        **kwargs
    }
    
    logger.info(f"OCPP event: {event_type}", extra=event_data)

def log_security_event(event_type: str, source_ip: str = None, user_id: str = None, **kwargs):
    """Логирование событий безопасности"""
    logger = logging.getLogger("security")
    
    event_data = {
        "event_type": event_type,
        "source_ip": source_ip,
        "user_id": user_id,
        **kwargs
    }
    
    logger.warning(f"Security event: {event_type}", extra=event_data) 