import os
from typing import Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database settings for Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "https://fsoffzrngojgsigrmlui.supabase.co")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZzb2ZmenJuZ29qZ3NpZ3JtbHVpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDc3NDI0MjgsImV4cCI6MjA2MzMxODQyOH0.sYUOvl85imQdQx0Z5SAs3VsuDwM9Lg7t56lQTSAUqSE")
    
    # PostgreSQL connection URL for Supabase
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", 
        "postgresql://postgres.fsoffzrngojgsigrmlui:YourActualPassword@aws-0-eu-north-1.pooler.supabase.com:6543/postgres"
    )
    
    # Redis settings
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
    
    # API settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "EvPower OCPP Backend"
    
    # Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-here")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # Legacy JWT settings (backward compatibility)
    JWT_SECRET_KEY: Optional[str] = None
    JWT_ALGORITHM: Optional[str] = None
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: Optional[str] = None
    
    # App settings
    APP_HOST: Optional[str] = "0.0.0.0"
    APP_PORT: Optional[str] = "8000"
    
    # CORS
    ALLOWED_HOSTS: str = os.getenv("ALLOWED_HOSTS", "*")
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000")
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_PATH: str = os.getenv("LOG_PATH", "/tmp/logs")
    
    class Config:
        env_file = ".env"
        extra = "ignore"  # Игнорируем дополнительные поля

settings = Settings() 