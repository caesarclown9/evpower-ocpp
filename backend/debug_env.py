#!/usr/bin/env python3
"""
Диагностический скрипт для проверки переменных окружения
"""
import os
from pathlib import Path

print("=== ДИАГНОСТИКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===")

# Проверяем наличие .env файла
env_path = Path(__file__).parent / '.env'
print(f"Путь к .env файлу: {env_path}")
print(f".env файл существует: {env_path.exists()}")

if env_path.exists():
    print(f"Размер .env файла: {env_path.stat().st_size} байт")

# Проверяем ключевые переменные
key_vars = [
    'DATABASE_URL',
    'SUPABASE_URL', 
    'SUPABASE_KEY',
    'PAYMENT_PROVIDER',
    'OBANK_USE_PRODUCTION',
    'ODENGI_USE_PRODUCTION'
]

print("\n=== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ===")
for var in key_vars:
    value = os.getenv(var)
    if value:
        # Маскируем чувствительные данные
        if 'URL' in var or 'KEY' in var or 'PASSWORD' in var:
            masked_value = value[:10] + "..." + value[-10:] if len(value) > 20 else value[:5] + "..."
            print(f"{var}: {masked_value}")
        else:
            print(f"{var}: {value}")
    else:
        print(f"{var}: НЕ УСТАНОВЛЕНА")

print(f"\nВсего переменных окружения: {len(os.environ)}")

# Проверяем могут ли мы импортировать settings
try:
    from app.core.config import settings
    print(f"\n✅ Settings импортированы успешно")
    print(f"DATABASE_URL из settings: {settings.DATABASE_URL[:20]}..." if settings.DATABASE_URL else "НЕ УСТАНОВЛЕН")
    print(f"PAYMENT_PROVIDER: {settings.PAYMENT_PROVIDER}")
except Exception as e:
    print(f"\n❌ Ошибка импорта settings: {e}")

# Попробуем создать database connection
try:
    from app.db.session import get_session_local
    SessionLocal = get_session_local()
    print(f"\n✅ SessionLocal создан успешно")
    
    db = SessionLocal()
    print(f"✅ Database connection создан")
    db.close()
    print(f"✅ Database connection закрыт")
    
except Exception as e:
    print(f"\n❌ Ошибка создания database connection: {e}")

print("\n=== ДИАГНОСТИКА ЗАВЕРШЕНА ===") 