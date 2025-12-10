from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
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


# ========== Async Database Support ==========
_async_engine = None
_AsyncSessionLocal = None


def get_async_engine():
    """Ленивая инициализация async database engine"""
    global _async_engine
    if _async_engine is None:
        from app.core.config import settings
        DATABASE_URL = os.getenv('DATABASE_URL', settings.DATABASE_URL)
        # Преобразуем postgresql:// в postgresql+asyncpg://
        if DATABASE_URL.startswith("postgresql://"):
            DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

        _async_engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=5,
            max_overflow=10,
        )
    return _async_engine


def get_async_session_local():
    """Ленивая инициализация AsyncSessionLocal"""
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        _AsyncSessionLocal = async_sessionmaker(
            bind=get_async_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _AsyncSessionLocal


async def get_async_db():
    """Async dependency для FastAPI"""
    AsyncSessionLocal = get_async_session_local()
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
