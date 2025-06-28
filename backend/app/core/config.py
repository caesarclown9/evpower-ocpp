import os
from typing import Optional, Any
from pydantic_settings import BaseSettings
from pydantic import model_validator

class Settings(BaseSettings):
    # Database settings for Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    
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
    APP_PORT: int = int(os.getenv("APP_PORT", "9210"))  # Production порт
    
    # OCPP WebSocket settings (тот же порт что и API)
    OCPP_WS_PORT: int = int(os.getenv("OCPP_WS_PORT", "9210"))
    OCPP_PROTOCOL_VERSION: str = os.getenv("OCPP_PROTOCOL_VERSION", "1.6")
    
    # CORS для FlutterFlow и внешних приложений
    ALLOWED_HOSTS: str = os.getenv("ALLOWED_HOSTS", "*")
    CORS_ORIGINS: str = os.getenv(
        "CORS_ORIGINS", 
        "http://localhost:3000,http://localhost:9210,https://app.flutterflow.io,https://ocpp.evpower.kg,http://ocpp.evpower.kg,ws://ocpp.evpower.kg,wss://ocpp.evpower.kg"
    )
    
    # OBANK Payment API Configuration  
    OBANK_API_URL: str = os.getenv("OBANK_API_URL", "http://test-rakhmet.dengi.kg/external/extended-cert")
    OBANK_PRODUCTION_API_URL: str = os.getenv("OBANK_PRODUCTION_API_URL", "https://rakhmet.dengi.kg:4431/external/extended-cert")
    OBANK_POINT_ID: str = os.getenv("OBANK_POINT_ID", "4354")  # Terminal ID
    OBANK_SERVICE_ID: str = os.getenv("OBANK_SERVICE_ID", "1331")  # Service ID
    OBANK_CERT_PATH: str = os.getenv("OBANK_CERT_PATH", "")  # Path to PKCS12 certificate
    OBANK_CERT_PASSWORD: str = os.getenv("OBANK_CERT_PASSWORD", "")  # Certificate password
    OBANK_USE_PRODUCTION: bool = os.getenv("OBANK_USE_PRODUCTION", "false").lower() == "true"
    
    # OBANK Production настройки
    OBANK_PROD_POINT_ID: str = os.getenv("OBANK_PROD_POINT_ID", "")
    OBANK_PROD_SERVICE_ID: str = os.getenv("OBANK_PROD_SERVICE_ID", "")
    
    # O!Dengi API Configuration (Legacy support)
    ODENGI_API_URL: str = os.getenv("ODENGI_API_URL", "https://mw-api-test.dengi.kg/api/json/json.php")
    ODENGI_PRODUCTION_API_URL: str = os.getenv("ODENGI_PRODUCTION_API_URL", "https://mw-api.dengi.kg/api/json/json.php")
    ODENGI_MERCHANT_ID: str = os.getenv("ODENGI_MERCHANT_ID", "")
    ODENGI_PASSWORD: str = os.getenv("ODENGI_PASSWORD", "")
    ODENGI_WEBHOOK_SECRET: Optional[str] = os.getenv("ODENGI_WEBHOOK_SECRET")
    ODENGI_USE_PRODUCTION: bool = os.getenv("ODENGI_USE_PRODUCTION", "false").lower() == "true"
    
    # O!Dengi Production настройки (получить у O!Dengi при регистрации merchant)
    ODENGI_PROD_MERCHANT_ID: str = os.getenv("ODENGI_PROD_MERCHANT_ID", "")
    ODENGI_PROD_PASSWORD: str = os.getenv("ODENGI_PROD_PASSWORD", "")
    
    # Payment Provider Selection
    PAYMENT_PROVIDER: str = os.getenv("PAYMENT_PROVIDER", "OBANK")  # OBANK or ODENGI
    
    # EZS Payment Settings
    EZS_SECRET_KEY: str = os.getenv("EZS_SECRET_KEY", "")
    DEFAULT_CURRENCY: str = os.getenv("DEFAULT_CURRENCY", "KGS")
    PAYMENT_TIMEOUT_MINUTES: int = int(os.getenv("PAYMENT_TIMEOUT_MINUTES", "30"))
    
    # Payment Lifecycle Settings
    QR_CODE_LIFETIME_MINUTES: int = int(os.getenv("QR_CODE_LIFETIME_MINUTES", "5"))
    INVOICE_LIFETIME_MINUTES: int = int(os.getenv("INVOICE_LIFETIME_MINUTES", "5"))
    STATUS_CHECK_INTERVAL_SECONDS: int = int(os.getenv("STATUS_CHECK_INTERVAL_SECONDS", "60"))
    CLEANUP_INTERVAL_MINUTES: int = int(os.getenv("CLEANUP_INTERVAL_MINUTES", "5"))
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_PATH: str = os.getenv("LOG_PATH", "/var/log/evpower-ocpp")
    
    # Environment
    APP_ENV: str = os.getenv("APP_ENV", "development")
    
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"
    
    @property
    def current_obank_api_url(self) -> str:
        """Возвращает актуальный URL OBANK API в зависимости от окружения"""
        if self.OBANK_USE_PRODUCTION:
            return self.OBANK_PRODUCTION_API_URL
        else:
            # Принудительно используем HTTP для тестового режима
            url = self.OBANK_API_URL
            if url.startswith("https://"):
                url = url.replace("https://", "http://")
                print(f"🔓 Тестовый режим: принудительно переключаем на HTTP: {url}")
            return url
    
    @property
    def current_obank_point_id(self) -> str:
        """Возвращает актуальный Point ID в зависимости от окружения"""
        return self.OBANK_PROD_POINT_ID if self.OBANK_USE_PRODUCTION else self.OBANK_POINT_ID
    
    @property
    def current_obank_service_id(self) -> str:
        """Возвращает актуальный Service ID в зависимости от окружения"""
        return self.OBANK_PROD_SERVICE_ID if self.OBANK_USE_PRODUCTION else self.OBANK_SERVICE_ID
    
    @model_validator(mode='after')
    def validate_settings(self) -> 'Settings':
        """Валидация обязательных переменных окружения"""
        missing_vars = []
        
        # Базовые переменные проверяем только если они критичны
        if not self.SECRET_KEY:
            missing_vars.append("SECRET_KEY")
        
        # DATABASE_URL должен быть обязательно
        if not self.DATABASE_URL:
            missing_vars.append("DATABASE_URL")
            
        # Проверяем переменные в зависимости от выбранного провайдера
        if self.PAYMENT_PROVIDER == "OBANK":
            if self.OBANK_USE_PRODUCTION:
                if not self.OBANK_PROD_POINT_ID:
                    missing_vars.append("OBANK_PROD_POINT_ID")
                if not self.OBANK_PROD_SERVICE_ID:
                    missing_vars.append("OBANK_PROD_SERVICE_ID")
            # Для тестовой среды не требуем сертификат - можем работать без него
                
        elif self.PAYMENT_PROVIDER == "ODENGI":
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
        
        return self

    class Config:
        extra = "ignore"

settings = Settings() 