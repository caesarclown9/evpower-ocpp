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
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
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
    ODENGI_MERCHANT_ID: str = os.getenv("ODENGI_MERCHANT_ID", "")
    ODENGI_PASSWORD: str = os.getenv("ODENGI_PASSWORD", "")
    ODENGI_WEBHOOK_SECRET: Optional[str] = os.getenv("ODENGI_WEBHOOK_SECRET")
    ODENGI_USE_PRODUCTION: bool = os.getenv("ODENGI_USE_PRODUCTION", "false").lower() == "true"
    
    # O!Dengi Production настройки (получить у O!Dengi при регистрации merchant)
    ODENGI_PROD_MERCHANT_ID: str = os.getenv("ODENGI_PROD_MERCHANT_ID", "")
    ODENGI_PROD_PASSWORD: str = os.getenv("ODENGI_PROD_PASSWORD", "")
    
    # EZS Payment Settings
    EZS_SECRET_KEY: str = os.getenv("EZS_SECRET_KEY", "")
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
    
    def __post_init__(self):
        """Валидация обязательных переменных окружения"""
        missing_vars = []
        
        # Обязательные переменные для работы
        if not self.SECRET_KEY:
            missing_vars.append("SECRET_KEY")
        if not self.EZS_SECRET_KEY:
            missing_vars.append("EZS_SECRET_KEY")
        if not self.DATABASE_URL:
            missing_vars.append("DATABASE_URL")
            
        # O!Dengi обязательные переменные
        if self.ODENGI_USE_PRODUCTION:
            if not self.ODENGI_PROD_MERCHANT_ID:
                missing_vars.append("ODENGI_PROD_MERCHANT_ID")
            if not self.ODENGI_PROD_PASSWORD:
                missing_vars.append("ODENGI_PROD_PASSWORD")
        else:
            if not self.ODENGI_MERCHANT_ID:
                missing_vars.append("ODENGI_MERCHANT_ID")
            if not self.ODENGI_PASSWORD:
                missing_vars.append("ODENGI_PASSWORD")
        
        if missing_vars:
            raise ValueError(f"❌ Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings() 