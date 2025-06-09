from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

# Ленивая инициализация database engine
_engine = None
_SessionLocal = None

def get_engine():
    """Ленивая инициализация database engine"""
    global _engine
    if _engine is None:
        from app.core.config import settings
        # Используем DATABASE_URL из настроек Supabase
        DATABASE_URL = os.getenv('DATABASE_URL', settings.DATABASE_URL)
        
        _engine = create_engine(
            DATABASE_URL,
            echo=False,
            future=True,
            pool_pre_ping=True,  # Проверка соединений перед использованием
            pool_recycle=300,    # Обновление соединений каждые 5 минут
            pool_size=5,         # Размер пула соединений
            max_overflow=10      # Максимальное количество дополнительных соединений
        )
    return _engine

def get_session_local():
    """Ленивая инициализация SessionLocal"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False
        )
    return _SessionLocal

# Dependency для FastAPI
# Для sync-режима

def get_db():
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
