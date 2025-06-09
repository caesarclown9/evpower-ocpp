#!/usr/bin/env python3
import os

def load_env_file(env_path='.env'):
    """Простая загрузка .env файла без внешних зависимостей"""
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
                    # Устанавливаем в os.environ если еще не установлено
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = value.strip()
    return env_vars

print("=== DEBUG ENVIRONMENT VARIABLES ===")

# Проверяем .env файл
print(f"Current directory: {os.getcwd()}")
print(f".env file exists: {os.path.exists('.env')}")

# Загружаем .env
env_vars = load_env_file()
print(f"Loaded {len(env_vars)} variables from .env")

print("\n=== ENVIRONMENT VARIABLES ===")
print(f"DATABASE_URL: {os.getenv('DATABASE_URL', 'NOT SET')}")
print(f"PAYMENT_PROVIDER: {os.getenv('PAYMENT_PROVIDER', 'NOT SET')}")
secret_key = os.getenv('SECRET_KEY', 'NOT SET')
print(f"SECRET_KEY (first 10 chars): {secret_key[:10] if secret_key != 'NOT SET' else 'NOT SET'}...")
print(f"REDIS_URL: {os.getenv('REDIS_URL', 'NOT SET')}")

# Проверяем содержимое .env файла
if os.path.exists('.env'):
    print("\n=== .ENV FILE CONTENT (first 20 lines) ===")
    with open('.env', 'r') as f:
        lines = f.readlines()[:20]
        for i, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                print(f"{i:2d}: {line}")
                continue
                
            # Скрываем пароли и секреты
            if any(keyword in line.lower() for keyword in ['password', 'secret', 'key']):
                parts = line.split('=', 1)
                if len(parts) >= 2:
                    print(f"{i:2d}: {parts[0]}=***HIDDEN***")
                else:
                    print(f"{i:2d}: {line}")
            else:
                print(f"{i:2d}: {line}")

# Проверяем что DATABASE_URL не содержит localhost
database_url = os.getenv('DATABASE_URL', '')
if 'localhost' in database_url.lower():
    print("\n⚠️  WARNING: DATABASE_URL contains 'localhost' - this should be Supabase URL!")
elif 'supabase.co' in database_url.lower():
    print("\n✅ DATABASE_URL looks like Supabase URL")
elif database_url:
    print("\n❓ DATABASE_URL is set but doesn't look like Supabase or localhost")
else:
    print("\n❌ DATABASE_URL is not set!")

print("\n=== TROUBLESHOOTING TIPS ===")
if not database_url:
    print("1. Add DATABASE_URL to .env file")
    print("2. Format: DATABASE_URL=postgresql://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres")
elif 'localhost' in database_url:
    print("1. Replace localhost with your Supabase URL")
    print("2. Check .env file for correct DATABASE_URL") 