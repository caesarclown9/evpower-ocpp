import os
from typing import Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database settings for Supabase
    SUPABASE_URL: str = "https://fsoffzrngojgsigrmlui.supabase.co"
    SUPABASE_ANON_KEY: str = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZzb2ZmenJuZ29qZ3NpZ3JtbHVpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDc3NDI0MjgsImV4cCI6MjA2MzMxODQyOH0.sYUOvl85imQdQx0Z5SAs3VsuDwM9Lg7t56lQTSAUqSE"
    
    # PostgreSQL connection URL for Supabase
    DATABASE_URL: str = "postgresql://postgres.fsoffzrngojgsigrmlui:Arma2000@aws-0-eu-north-1.pooler.supabase.com:6543/postgres"
    
    # Redis settings
    REDIS_URL: str = "redis://localhost:6379"
    
    # API settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "EvPower OCPP Backend"
    
    # Security
    SECRET_KEY: str = "your-secret-key-here"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # Legacy JWT settings (backward compatibility)
    JWT_SECRET_KEY: Optional[str] = None
    JWT_ALGORITHM: Optional[str] = None
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: Optional[str] = None
    
    # App settings
    APP_HOST: Optional[str] = "0.0.0.0"
    APP_PORT: Optional[str] = "8000"
    
    # CORS
    ALLOWED_HOSTS: str = "*"
    CORS_ORIGINS: str = ""
    
    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_PATH: str = "/var/log/ocpp"
    
    class Config:
        env_file = ".env"
        extra = "ignore"  # Игнорируем дополнительные поля

settings = Settings() 