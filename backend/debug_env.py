#!/usr/bin/env python3
import os
from dotenv import load_dotenv

print("=== DEBUG ENVIRONMENT VARIABLES ===")

# Проверяем .env файл
print(f"Current directory: {os.getcwd()}")
print(f".env file exists: {os.path.exists('.env')}")

# Загружаем .env
load_dotenv()

print("\n=== ENVIRONMENT VARIABLES ===")
print(f"DATABASE_URL: {os.getenv('DATABASE_URL', 'NOT SET')}")
print(f"PAYMENT_PROVIDER: {os.getenv('PAYMENT_PROVIDER', 'NOT SET')}")
print(f"SECRET_KEY (first 10 chars): {(os.getenv('SECRET_KEY', 'NOT SET'))[:10]}...")
print(f"REDIS_URL: {os.getenv('REDIS_URL', 'NOT SET')}")

# Проверяем импорт settings
try:
    import sys
    sys.path.append('.')
    from app.core.config import settings
    print("\n=== SETTINGS OBJECT ===")
    print(f"settings.DATABASE_URL: {settings.DATABASE_URL}")
    print(f"settings.PAYMENT_PROVIDER: {settings.PAYMENT_PROVIDER}")
except Exception as e:
    print(f"\nError importing settings: {e}")

# Проверяем содержимое .env файла
if os.path.exists('.env'):
    print("\n=== .ENV FILE CONTENT (first 20 lines) ===")
    with open('.env', 'r') as f:
        lines = f.readlines()[:20]
        for i, line in enumerate(lines, 1):
            # Скрываем пароли и секреты
            if any(keyword in line.lower() for keyword in ['password', 'secret', 'key']):
                parts = line.split('=')
                if len(parts) >= 2:
                    print(f"{i:2d}: {parts[0]}=***HIDDEN***")
                else:
                    print(f"{i:2d}: {line.strip()}")
            else:
                print(f"{i:2d}: {line.strip()}") 