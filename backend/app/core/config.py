import os
from typing import Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database settings for Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
    
    # PostgreSQL connection URL for Supabase
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    
    # Redis settings
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
    
    # API settings
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "EvPower OCPP Backend"
    
    # Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-here")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # App settings - правильные порты для production
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8180"))  # Production порт
    
    # OCPP WebSocket settings
    OCPP_WS_PORT: int = int(os.getenv("OCPP_WS_PORT", "8180"))
    OCPP_PROTOCOL_VERSION: str = os.getenv("OCPP_PROTOCOL_VERSION", "1.6")
    
    # CORS для FlutterFlow и внешних приложений
    ALLOWED_HOSTS: str = os.getenv("ALLOWED_HOSTS", "*")
    CORS_ORIGINS: str = os.getenv(
        "CORS_ORIGINS", 
        "http://localhost:3000,http://localhost:8180,https://app.flutterflow.io"
    )
    
    # O!Dengi API Configuration
    ODENGI_API_URL: str = os.getenv("ODENGI_API_URL", "https://mw-api-test.dengi.kg/api/json/json.php")
    ODENGI_PRODUCTION_API_URL: str = os.getenv("ODENGI_PRODUCTION_API_URL", "https://mw-api.dengi.kg/api/json/json.php")
    ODENGI_MERCHANT_ID: str = os.getenv("ODENGI_MERCHANT_ID", "4672496329")
    ODENGI_PASSWORD: str = os.getenv("ODENGI_PASSWORD", "F7XFAI4O5A@CS2W")
    ODENGI_WEBHOOK_SECRET: Optional[str] = os.getenv("ODENGI_WEBHOOK_SECRET")
    ODENGI_USE_PRODUCTION: bool = os.getenv("ODENGI_USE_PRODUCTION", "false").lower() == "true"
    
    # EZS Payment Settings
    EZS_SECRET_KEY: str = os.getenv("EZS_SECRET_KEY", "evpower_secret_key_2024")
    DEFAULT_CURRENCY: str = os.getenv("DEFAULT_CURRENCY", "KGS")
    PAYMENT_TIMEOUT_MINUTES: int = int(os.getenv("PAYMENT_TIMEOUT_MINUTES", "30"))
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_PATH: str = os.getenv("LOG_PATH", "/var/log/evpower-ocpp")
    
    # Environment
    APP_ENV: str = os.getenv("APP_ENV", "development")
    
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"
    
    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings() 