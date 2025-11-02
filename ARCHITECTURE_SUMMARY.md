# EvPower Backend - Краткое резюме архитектуры

## 1️⃣ Быстрый старт

**Проект:** Асинхронный WebSocket сервер для управления сетью EV зарядных станций
**Язык:** Python 3.11+ (FastAPI)
**Главный порт:** 9210
**Протокол:** OCPP 1.6 JSON + REST API v1

## 2️⃣ Директория приложения

```
backend/app/
├── main.py                    ← Entry point (конфиг, middleware, lifespan)
├── api/v1/                    ← REST API endpoints
│   ├── charging/              ← Запуск/остановка зарядки
│   ├── balance/               ← Пополнение баланса
│   ├── payment/               ← Webhook платежей
│   ├── station/               ← Статус станций
│   └── locations/             ← Локации + WebSocket
├── core/                      ← Middleware + конфигурация
│   ├── config.py              ← Pydantic Settings (env vars)
│   ├── auth_middleware.py     ← JWT/HMAC аутентификация
│   └── security_middleware.py ← Rate limiting, security headers
├── db/                        ← PostgreSQL через SQLAlchemy
│   └── models/ocpp.py         ← ORM модели (User, Client, Station, etc.)
├── crud/                      ← CRUD операции (ocpp_service.py)
└── services/                  ← Бизнес-логика (платежи, тарифы, статусы)

ocpp_ws_server/               ← WebSocket OCPP сервер
├── ws_handler.py             ← OCPPChargePoint (OCPP 1.6 обработчик)
├── redis_manager.py          ← Redis async клиент (кэш + pub/sub)
└── client.py                 ← Низкоуровневый WebSocket клиент
```

## 3️⃣ Основные компоненты

### FastAPI приложение (main.py)
- **Middleware стек:** Auth → Idempotency → Security → PaymentAudit → CORS
- **Фоновые задачи:**
  - Payment status monitoring (per платеж)
  - Payment cleanup (каждый час)
  - Station status updates (каждые 2 мин)
  - Hanging session detection (каждые 30 мин)

### WebSocket OCPP (ws_handler.py)
- Маршруты: `/ws/{station_id}`, `/ocpp/{station_id}`
- Обрабатываемые OCPP сообщения (Tier 1-3):
  - BootNotification, Heartbeat, Authorize
  - StartTransaction, StopTransaction, MeterValues
  - RemoteStartTransaction, RemoteStopTransaction

### Redis (redis_manager.py)
- Station registration/unregistration
- Pub/Sub для команд (RemoteStart, RemoteStop)
- Transaction caching
- Data caching с TTL

### API v1 endpoints
```
Balance:    POST /api/v1/balance/topup-qr, /topup-card, GET /current
Charging:   POST /api/v1/charging/start, /stop, GET /{session_id}
Payment:    POST /api/v1/payment/webhook, /h2h-create, GET /status
Station:    GET /api/v1/stations, /{id}/status
Locations:  GET /api/v1/locations, WS /stream
```

## 4️⃣ Потоки данных

### Пополнение баланса (QR)
```
POST /balance/topup-qr 
  → PaymentProvider.create_invoice() 
    → O!Dengi API 
      → Возвращает QR код + invoice_id
        → Клиент сканирует QR
          → O!Dengi webhook
            → POST /payment/webhook
              → Update balance_topup (approved)
                → Update clients.balance (+ сумма)
```

### Запуск зарядки
```
POST /charging/start 
  → ChargingService.start_charging_session()
    → Проверка баланса, резервирование средств
      → Create charging_session
        → Redis: publish_command(station_id, RemoteStartTransaction)
          → Станция получает команду и стартует зарядку
            → Station: StartTransaction OCPP
              → ws_handler: save meter_start + transaction_id
```

### Остановка зарядки
```
Station: StopTransaction OCPP
  → ws_handler: calculate energy_delivered
    → Calculate amount = energy × price
      → Update charging_session (status=stopped)
        → Calculate refund = reserved - actual
          → Update clients.balance (+ refund)
```

### Мониторинг зависших сессий (v1.2.2)
```
Scheduler (каждые 30 мин)
  → Check 2 типа проблем:
    1. Sessions > 12 часов
    2. Sessions без OCPP tx > 10 мин (NEW)
      → Stop charging
        → Вернуть все средства (refund = reserved)
          → Update balance + log reason
```

## 5️⃣ Безопасность

| Компонент | Реализация |
|-----------|-----------|
| **Аутентификация** | JWT RS256 (Supabase) + HMAC SHA256 (fallback) |
| **Rate Limiting** | 60 req/min (default), 10 req/min (charging/balance) |
| **Webhook DDoS** | 30 req/min на /payment/webhook |
| **Idempotency** | Redis (24h) + Idempotency-Key header |
| **Payment Audit** | Логирование всех платежей (маскирование сумм) |
| **SQL Injection** | SQLAlchemy ORM + параметризованные запросы |
| **SSL/TLS** | HTTPS/WSS через Traefik (Let's Encrypt) |
| **RLS** | Row Level Security в Supabase (per-client visibility) |

## 6️⃣ Технический стек

**Основные зависимости:**
```
FastAPI >= 0.104.0       - Web framework
Uvicorn >= 0.24.0        - ASGI server
SQLAlchemy >= 2.0.0      - ORM
Pydantic >= 2.5.0        - Validation
Redis                    - Async cache + pub/sub
Websockets               - WebSocket protocol
OCPP                     - OCPP 1.6 protocol
APScheduler >= 3.10.0    - Scheduled tasks
python-jose              - JWT handling
```

**Версии:**
- Python: 3.11+
- FastAPI: ^0.104.0
- SQLAlchemy: ^2.0.0
- Pydantic: ^2.5.0

## 7️⃣ Развертывание

**Docker:**
```yaml
Multi-stage Dockerfile:
  Stage 1: Builder (gcc, dependencies)
  Stage 2: Runtime (python:3.11-slim, non-root user)
  
docker-compose:
  Services: redis (alpine, 256MB), evpower-backend (FastAPI)
  Port: 9210
  Health check: curl /health
```

**Environment Variables (обязательные):**
```bash
DATABASE_URL=postgresql://...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_JWT_SECRET=xxx
REDIS_URL=redis://redis:6379/0
PAYMENT_PROVIDER=ODENGI
ODENGI_MERCHANT_ID=xxx
ODENGI_PASSWORD=xxx
```

## 8️⃣ v1.2.2 Критические исправления (2025-11-02)

| Проблема | Решение | Статус |
|----------|---------|--------|
| 🔴 Race condition в платежах | Убрана проверка статуса при webhook | ✅ FIXED |
| 🔴 Блокировка на 12 часов | Auto-stop через 10 мин без подключения | ✅ FIXED |
| 🟡 Противоречие taimeout | Unified на 5 минут (QR + invoice) | ✅ FIXED |

## 9️⃣ Главные файлы для изучения

1. **main.py** - Entry point, middleware, background tasks
2. **ws_handler.py** - OCPP обработчик (StartTransaction, StopTransaction)
3. **api/v1/charging/service.py** - Бизнес-логика зарядки
4. **api/v1/balance/topup.py** - QR платежи
5. **api/v1/payment/webhook.py** - Webhook обработка
6. **core/auth_middleware.py** - JWT аутентификация
7. **core/security_middleware.py** - Rate limiting
8. **core/config.py** - Конфигурация приложения

## 🔟 Запуск

**Развитие:**
```bash
# Установить зависимости
pip install -r requirements.txt

# Запустить с docker-compose
docker-compose up

# Приложение доступно на http://localhost:9210
# WebSocket на ws://localhost:9210/ws/{station_id}
# Документация на http://localhost:9210/docs (если ENABLE_SWAGGER=true)
```

**Production:**
```bash
# Использовать docker-compose.production.yml
docker-compose -f docker-compose.production.yml up

# С Traefik (через Coolify):
# Domain: ocpp.evpower.kg
# TLS: auto (Let's Encrypt)
# WebSocket: поддержка через X-Forwarded-Proto headers
```

---

## Полная документация

Подробный анализ архитектуры с диаграммами и примерами кода:
→ `/mnt/d/Projects/EvPower-Backend/ARCHITECTURE.md`

