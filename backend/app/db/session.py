from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv
from app.core.config import settings

load_dotenv()

# Используем DATABASE_URL из настроек Supabase
DATABASE_URL = os.getenv('DATABASE_URL', settings.DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,  # Проверка соединений перед использованием
    pool_recycle=300,    # Обновление соединений каждые 5 минут
    pool_size=5,         # Размер пула соединений
    max_overflow=10      # Максимальное количество дополнительных соединений
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False
)

# Dependency для FastAPI
# Для sync-режима

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
