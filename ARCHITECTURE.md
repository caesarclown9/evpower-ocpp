# EvPower Backend - Анализ Архитектуры

## Содержание
1. [Обзор проекта](#обзор-проекта)
2. [Общая структура](#общая-структура)
3. [Основные компоненты](#основные-компоненты)
4. [Архитектурные паттерны](#архитектурные-паттерны)
5. [Потоки данных](#потоки-данных)
6. [Безопасность](#безопасность)
7. [Развертывание](#развертывание)

---

## Обзор проекта

**EvPower Backend** - это асинхронный WebSocket сервер для управления сетью зарядных станций электромобилей.

### Ключевые характеристики:
- **Язык:** Python 3.11+
- **Фреймворк:** FastAPI + Uvicorn
- **Протокол:** OCPP 1.6 JSON (Open Charge Point Protocol)
- **База данных:** Supabase PostgreSQL
- **Кэширование:** Redis (асинхронный)
- **Размер кода:** ~8,850 строк Python
- **Версия:** 1.2.2 (с критическими исправлениями платежей)

### Основная функциональность:
- WebSocket соединение с зарядными станциями (OCPP 1.6)
- Управление сессиями зарядки
- Платежная интеграция (O!Dengi, OBANK - на паузе)
- Управление балансом клиентов
- Мониторинг станций в реальном времени
- Фоновые задачи (очистка платежей, проверка зависших сессий)

---

## Общая структура

```
backend/
├── app/                          # Основное приложение FastAPI
│   ├── main.py                  # Entry point, конфигурация приложения
│   ├── api/                     # API endpoints
│   │   ├── v1/                  # API версия 1 (модульная архитектура)
│   │   │   ├── balance/         # Управление балансом
│   │   │   ├── charging/        # Управление зарядкой
│   │   │   ├── payment/         # Платежи и вебхуки
│   │   │   ├── station/         # Информация о станциях
│   │   │   ├── locations/       # Локации и WebSocket
│   │   │   └── profile.py       # Профиль пользователя
│   │   └── mobile.py            # Legacy Mobile API (FlutterFlow)
│   │
│   ├── core/                    # Ядро приложения
│   │   ├── config.py            # Конфигурация (Pydantic Settings)
│   │   ├── auth_middleware.py   # JWT/HMAC аутентификация
│   │   ├── security_middleware.py # Rate limiting, CSP, security headers
│   │   ├── idempotency_middleware.py # Защита от дублирования
│   │   ├── payment_audit.py     # Аудит платежей
│   │   ├── station_auth.py      # Аутентификация станций
│   │   ├── logging_config.py    # Логирование
│   │   ├── health_checks.py     # Health checks
│   │   └── graceful_shutdown.py # Graceful shutdown
│   │
│   ├── db/                      # Слой базы данных
│   │   ├── session.py           # SQLAlchemy сессия (ленивая инициализация)
│   │   ├── base.py              # Base класс для моделей
│   │   ├── base_class.py        # Mixin базовый класс
│   │   └── models/
│   │       └── ocpp.py          # SQLAlchemy ORM модели
│   │
│   ├── crud/                    # CRUD операции
│   │   ├── ocpp.py              # OCPP запросы
│   │   └── ocpp_service.py      # Сервисы OCPP (платежи, станции, транзакции)
│   │
│   ├── services/                # Бизнес-логика сервисов
│   │   ├── payment_provider_service.py # Унифицированный провайдер платежей
│   │   ├── pricing_service.py   # Расчет тарифов
│   │   ├── obank_service.py     # OBANK интеграция (на паузе)
│   │   ├── realtime_service.py  # Real-time обновления
│   │   ├── location_status_service.py # Статус локаций
│   │   ├── station_status_manager.py # Управление статусом станций
│   │   └── common_crud.py       # Общие CRUD операции
│   │
│   └── schemas/                 # Pydantic схемы валидации
│       └── ocpp.py              # OCPP запрос/ответ схемы
│
├── ocpp_ws_server/              # WebSocket OCPP сервер
│   ├── ws_handler.py            # Основной WebSocket обработчик (OCPPChargePoint)
│   ├── client.py                # Клиент соединение
│   ├── server.py                # Базовая реализация сервера (legacy)
│   ├── redis_manager.py         # Redis асинхронный менеджер
│   └── __init__.py
│
├── migrations/                  # SQL миграции для Supabase
│   ├── 001_enable_rls_security.sql
│   ├── 002_add_location_status_view.sql
│   └── add_station_availability.sql
│
├── tests/                       # Unit тесты
├── certificates/               # SSL сертификаты (OBANK)
├── requirements.txt            # Python зависимости
├── .env.example               # Шаблон переменных окружения
└── Dockerfile.production      # Production Docker образ

docker-compose.yml            # Локальная разработка
docker-compose.production.yml # Production разворачивание
```

### Статистика файлов:

| Категория | Файлы | Назначение |
|-----------|-------|-----------|
| API endpoints | 17+ | REST эндпоинты v1 |
| Core middleware | 8 | Аутентификация, безопасность, логирование |
| Services | 7 | Бизнес-логика |
| CRUD/ORM | 5 | Работа с БД |
| WebSocket | 4 | OCPP протокол |
| Config/Schema | 3 | Конфигурация и валидация |

---

## Основные компоненты

### 1. FastAPI приложение (`main.py`)

**Порт:** 9210

**Функции:**
- Конфигурация CORS, middleware
- Регистрация роутеров API
- Управление жизненным циклом (lifespan)
- Фоновые задачи

**Middleware (в порядке применения):**
```
1. AuthMiddleware         - JWT/HMAC аутентификация
2. IdempotencyMiddleware  - Защита от дублирования
3. SecurityMiddleware     - Rate limiting, security headers
4. PaymentAuditMiddleware - Логирование платежей
5. CORSMiddleware         - CORS
```

**Фоновые задачи:**
1. `check_payment_status()` - Мониторинг статуса платежей (запускается при создании платежа)
2. `payment_cleanup_task()` - Периодическая очистка просроченных платежей (каждый час)
3. APScheduler jobs:
   - `update_station_statuses_job()` - каждые 2 минуты
   - `check_hanging_sessions_job()` - каждые 30 минут

### 2. WebSocket OCPP Сервер (`ocpp_ws_server/`)

**Маршруты:**
```
GET  /health                      - Health check
GET  /readyz                      - Readiness probe
POST /api/v1/* (все API v1)       - REST API endpoints
WS   /ws/{station_id}             - OCPP WebSocket (основной)
WS   /ocpp/{station_id}           - OCPP WebSocket (альтернативный)
```

**OCPPChargePoint (ws_handler.py) - реализует OCPP 1.6:**

```python
Tier 1 (критически важные):
├── BootNotification      - Регистрация станции
├── Heartbeat             - Проверка жизни
├── Authorize             - Авторизация клиента
├── StartTransaction      - Начало транзакции
├── StopTransaction       - Окончание транзакции
└── MeterValues           - Данные счетчика

Tier 2 (важные):
├── StatusNotification    - Уведомление о статусе
├── DiagnosticsStatusNotification - Диагностика
├── FirmwareStatusNotification    - Обновления прошивки
└── ReserveNow            - Бронирование

Tier 3 (административные):
├── Reset                 - Перезагрузка станции
├── ChangeConfiguration   - Изменение конфигурации
├── GetConfiguration      - Получение конфигурации
├── RemoteStartTransaction - Удаленный старт
└── RemoteStopTransaction  - Удаленная остановка
```

**Жизненный цикл WebSocket соединения:**

```
Station connects (ws_handler.py)
    ↓
ClientProtocol.ping() → set connection timeout
    ↓
BootNotification received
    ├─ Fast response (RegistrationStatus.accepted)
    └─ Background processing:
       ├─ Mark boot in DB
       ├─ Set configuration
       └─ Check pending sessions → RemoteStartTransaction
    ↓
Heartbeat (every 5 minutes)
    ├─ Update last_heartbeat_at
    └─ Mark station as available
    ↓
StartTransaction → StopTransaction (charging cycle)
    ├─ Validate connector
    ├─ Create charging_session
    ├─ Store meter values
    └─ Calculate energy used
    ↓
Station disconnects
    └─ Mark as unavailable (after heartbeat timeout)
```

### 3. API v1 Маршруты

#### **Balance Module** (`api/v1/balance/`)
```
POST /api/v1/balance/topup-qr      - Создать QR платеж
POST /api/v1/balance/topup-card    - Создать H2H платеж
GET  /api/v1/balance/current       - Получить текущий баланс
GET  /api/v1/balance/history       - История платежей
POST /api/v1/payment/check-status  - Проверить статус платежа
```

#### **Charging Module** (`api/v1/charging/`)
```
POST /api/v1/charging/start        - Начать зарядку
POST /api/v1/charging/stop         - Остановить зарядку
GET  /api/v1/charging/{session_id} - Статус сессии
GET  /api/v1/charging/history      - История зарядок
```

#### **Payment Module** (`api/v1/payment/`)
```
POST /api/v1/payment/webhook       - Webhook платежных уведомлений
POST /api/v1/payment/h2h-create    - Создать H2H платеж
GET  /api/v1/payment/status        - Получить статус платежа
POST /api/v1/payment/token         - Управление токеном платежей
```

#### **Station Module** (`api/v1/station/`)
```
GET  /api/v1/station/{id}/status   - Статус станции
GET  /api/v1/stations              - Список станций
```

#### **Locations Module** (`api/v1/locations/`)
```
GET  /api/v1/locations             - Список локаций
GET  /api/v1/locations/{id}        - Детали локации
WS   /api/v1/locations/stream      - Real-time обновления локаций
```

### 4. Redis Manager (`ocpp_ws_server/redis_manager.py`)

**Ключевые операции:**

```python
Станции:
├── register_station(station_id)     - Регистрация станции
├── unregister_station(station_id)   - Разрегистрация
└── get_stations()                   - Получить активные станции

Команды (Pub/Sub):
├── publish_command(station_id, cmd) - Отправить команду станции
└── listen_commands(station_id)      - Слушать команды для станции

Транзакции:
├── add_transaction(station_id, tx)  - Сохранить OCPP транзакцию
└── get_transactions(station_id)     - Получить транзакции

Кэширование:
├── cache_data(key, value, ttl)      - Кэшировать данные
├── get_cached_data(key)             - Получить из кэша
└── delete(key)                      - Удалить из кэша

Pub/Sub:
├── publish(channel, message)        - Опубликовать сообщение
└── get_message(channel)             - Получить сообщение
```

### 5. Аутентификация и Авторизация

#### **JWT (Supabase)**
- Алгоритмы: HS256 (legacy), RS256 (современный)
- Source: `Authorization: Bearer {token}`
- JWKS caching с TTL 1 час
- Верификация ISS, AUD, EXP

#### **HMAC (Fallback)**
- Headers: X-Client-Id, X-Client-Timestamp, X-Client-Signature
- Алгоритм: SHA256
- Защита от replay (timestamp validation)

#### **Station Auth**
- Master API Key (constant-time сравнение через `hmac.compare_digest()`)
- Используется для administrative endpoints

### 6. Модели Данных (ORM)

**Основные таблицы Supabase:**

```sql
-- Users & Clients
users              - Операторы системы
clients            - Клиенты сервиса (пользователи мобильного приложения)

-- Charging Infrastructure  
locations          - Локации зарядных станций
stations           - Зарядные станции (OCPP compatible)
connectors         - Разъемы на станциях

-- Charging Sessions & Payments
charging_sessions  - Сессии зарядки
ocpp_transactions  - Транзакции OCPP (BootNotification, Heartbeat, Start, Stop)
ocpp_meter_values  - Значения счетчика энергии
balance_topups     - Пополнение баланса клиентов

-- Tariffs & Pricing
tariff_plans       - Планы тарификации
tariff_rules       - Правила расчета цены

-- Payments
balance_topups     - История пополнений баланса
payment_transactions_odengi - Транзакции O!Dengi
```

---

## Архитектурные паттерны

### 1. **Слойная архитектура**

```
┌─────────────────────────────────────┐
│  API Layer (FastAPI Routers)        │
│  POST /api/v1/charging/start        │
├─────────────────────────────────────┤
│  Service Layer (Business Logic)     │
│  ChargingService, PricingService    │
├─────────────────────────────────────┤
│  Repository Layer (CRUD)            │
│  OCPPStationService, OCPPTx Service │
├─────────────────────────────────────┤
│  Data Access Layer (SQLAlchemy ORM) │
│  Session, Models, DB Connection     │
├─────────────────────────────────────┤
│  Database (Supabase PostgreSQL)     │
└─────────────────────────────────────┘
```

### 2. **Асинхронная архитектура**

```
FastAPI (async)
    ├── HTTP endpoints (async def)
    ├── WebSocket handlers (async def)
    └── Background tasks (asyncio.create_task)

Redis (async)
    ├── redis.asyncio - асинхронный клиент
    └── Pub/Sub для Real-time

Database (sync)
    ├── SQLAlchemy sync session (блокирует I/O)
    └── Ленивая инициализация (lazy loading)
```

### 3. **Dependency Injection**

```python
# FastAPI Dependencies
@router.post("/api/v1/charging/start")
async def start_charging(
    request: ChargingStartRequest,
    db: Session = Depends(get_db),           # DB инъекция
    http_request: Request = None             # HTTP инъекция
):
    ...
```

### 4. **Service Locator для платежей**

```python
payment_provider = get_payment_provider_service()

# Автоматически выбирает:
# - OBANK (если enabled и OBANK_ENABLED=true)
# - O!Dengi (по умолчанию, PAYMENT_PROVIDER=ODENGI)
```

### 5. **Фоновые задачи**

```
Типы:
├── Immediate Background (asyncio.create_task)
│   ├── check_payment_status() - per платеж
│   └── _handle_boot_notification_background()
│
├── Scheduled (APScheduler)
│   ├── update_station_statuses_job() - 2 мин
│   └── check_hanging_sessions_job() - 30 мин
│
└── Periodic (asyncio.sleep loop)
    └── payment_cleanup_task() - 1 час
```

---

## Потоки данных

### 1. **Поток пополнения баланса (QR платеж)**

```
Client (мобильное приложение)
    │
    └─→ POST /api/v1/balance/topup-qr
            │
            ├─ Валидация (клиент существует, нет активных QR)
            ├─ Создание order_id
            └─ Вызов PaymentProviderService.create_payment()
                    │
                    └─→ O!Dengi API (create_invoice)
                            │
                            └─→ Возвращает QR код + invoice_id
                    │
                    ├─ Сохранение balance_topup (status='processing')
                    ├─ Сохранение invoice_expires_at (QR код на 5 мин)
                    └─ Возвращение QR в клиент
                            │
                            └─ Клиент сканирует QR
                                    │
                                    └─ Платеж в O!Dengi
                                            │
                                            └─ O!Dengi отправляет webhook
                                                    │
                    ┌───────────────────────────────┘
                    │
        POST /api/v1/payment/webhook (O!Dengi)
            │
            ├─ Парсинг XML (O!Dengi отправляет XML)
            ├─ Верификация подписи
            └─ Обновление balance_topup:
                    │
                    ├─ status='approved'
                    ├─ paid_amount = сумма
                    └─ paid_at = NOW()
                    │
                    └─ Фоновая обработка:
                            │
                            ├─ Получить текущий баланс клиента
                            ├─ Прибавить paid_amount
                            ├─ Сохранить в payment_transactions_odengi
                            └─ Обновить clients.balance
```

### 2. **Поток запуска зарядки**

```
Client (мобильное приложение)
    │
    └─→ POST /api/v1/charging/start
            {
                "station_id": "CHR-BGK-001",
                "connector_id": 1,
                "energy_kwh": 50,        // или
                "amount_som": 500        // опционально
            }
            │
            ├─ Извлечение client_id из JWT/HMAC
            ├─ Валидация параметров (energy > 0, amount > 0)
            ├─ Проверка баланса клиента
            │
            ├─ Если energy_kwh:
            │   └─ Расчет цены = energy_kwh * price_per_kwh
            │
            ├─ Резервирование средств:
            │   ├─ (charge_amount - current_balance)
            │   ├─ Проверка баланса достаточен
            │   └─ UPDATE clients SET balance = balance - charge_amount
            │
            ├─ Создание charging_session:
            │   ├─ session_id = UUID
            │   ├─ status = 'started'
            │   ├─ energy_reserved = energy_kwh или расчет
            │   └─ amount_reserved = charge_amount
            │
            ├─ Отправка RemoteStartTransaction через Redis
            │   └─ publish_command(station_id, {
            │       "action": "RemoteStartTransaction",
            │       "connector_id": 1,
            │       "id_tag": "CLIENT_{client_id}",
            │       "session_id": session_id
            │   })
            │
            └─→ Возвращение session_id + статус клиенту
                    │
                    ↓
            Станция получает команду из Redis
                    │
                    ├─ Запускает зарядку на коннекторе
                    └─ Отправляет StartTransaction (OCPP)
                            │
                    ┌────────┘
                    │
        StartTransaction (OCPP) → ws_handler.py
            │
            ├─ Парсинг OCPP команды
            ├─ Извлечение connector_id, id_tag, meter_start
            ├─ Сохранение в Redis (ocpp:transactions:{station_id})
            └─ Возвращение transaction_id станции
```

### 3. **Поток остановки зарядки**

```
Client или AutoStop
    │
    └─→ POST /api/v1/charging/stop
            {
                "session_id": "uuid-xxxx"
            }
            │
            ├─ Отправка RemoteStopTransaction через Redis
            │
            └─→ Станция получает команду
                    │
                    ├─ Останавливает зарядку
                    └─ Отправляет StopTransaction (OCPP)
                            │
                    ┌────────┘
                    │
        StopTransaction (OCPP) → ws_handler.py
            │
            ├─ Парсинг: connector_id, meter_stop, transaction_id
            │
            ├─ Расчет:
            │   ├─ energy_delivered = meter_stop - meter_start
            │   ├─ amount = energy_delivered * price_per_kwh (если по энергии)
            │   └─ Можно вычитать из reserved, если переплата
            │
            ├─ Обновление charging_session:
            │   ├─ status = 'stopped'
            │   ├─ energy_consumed = energy_delivered
            │   ├─ amount_paid = actual_amount
            │   └─ stop_time = NOW()
            │
            ├─ Расчет возврата:
            │   ├─ refund_amount = amount_reserved - amount_paid
            │   ├─ Если refund_amount > 0:
            │   │   └─ UPDATE clients SET balance += refund_amount
            │   └─ Сохранить refund в payment_transactions
            │
            └─ Отправка уведомления клиенту (push notification)
```

### 4. **Поток мониторинга зависших сессий**

```
Scheduler (каждые 30 минут)
    │
    └─→ check_hanging_sessions_job()
            │
            ├─ Проверка 1: Сессии > 12 часов
            │   └─ SELECT charging_sessions WHERE
            │       status='started' AND
            │       created_at < NOW() - interval '12 hours'
            │
            ├─ Проверка 2: Сессии без подключения > 10 мин (NEW в v1.2.2)
            │   └─ SELECT charging_sessions LEFT JOIN ocpp_transactions
            │       WHERE status='started' AND
            │       no OCPP transaction AND
            │       created_at < NOW() - interval '10 minutes'
            │
            └─ Для каждой найденной сессии:
                    │
                    ├─ Остановить зарядку (RemoteStopTransaction)
                    ├─ Вернуть все средства:
                    │   └─ refund = amount_reserved
                    ├─ UPDATE charging_sessions:
                    │   ├─ status = 'stopped'
                    │   └─ refund_amount = amount_reserved
                    ├─ Обновить баланс клиента
                    └─ Логировать с причиной (NO_CONNECTION / TOO_LONG)
```

---

## Безопасность

### 1. **Аутентификация**

| Метод | Использование | Провайдер |
|-------|---------------|-----------|
| JWT RS256 | Mobile API | Supabase |
| JWT HS256 | Legacy clients | Supabase (deprecated) |
| HMAC SHA256 | Fallback | Custom |
| Master API Key | Administrative | Environment variable |

### 2. **Rate Limiting** (SecurityMiddleware)

```python
# Основной лимит (default)
RATE_LIMIT_DEFAULT_PER_MINUTE = 60  # Все endpoints

# Критичные операции (денежные)
RATE_LIMIT_CRITICAL_PER_MINUTE = 10
Endpoints:
├── POST /charging/start
├── POST /charging/stop
└── POST /balance/topup*

# Webhook защита от DDoS
RATE_LIMIT_WEBHOOK_PER_MINUTE = 30
Endpoints:
└── POST /payment/webhook
```

Алгоритм: **Sliding Window Counter** (деквалась для временных меток)

### 3. **Защита от повторных запросов** (IdempotencyMiddleware)

- Header: `Idempotency-Key`
- Кэширование в Redis на 24 часа
- Защита на endpoints с побочными эффектами (POST, PUT)

### 4. **Аудит платежей** (PaymentAuditMiddleware)

```python
Логирование каждого платежного запроса:
├── Timestamp
├── Client IP
├── Client ID
├── Amount (замаскирована)
├── Operation (charging/topup)
├── Status
└── Error (если есть)

Хранилище: Файлы логов + Supabase (audit_logs таблица)
```

### 5. **Security Headers** (SecurityMiddleware)

```http
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-XSS-Protection: 1; mode=block
Content-Security-Policy: ...
Strict-Transport-Security: max-age=31536000
```

### 6. **Protection от SQL Injection**

- Использование SQLAlchemy ORM
- Параметризованные запросы через `text()`
- Типизация в Pydantic моделях

### 7. **Protection от CSRF**

- Отсутствует (stateless REST API, используется JWT)
- Защита через Same-Site cookies (если будут)

### 8. **SSL/TLS**

- Production: HTTPS through Traefik (Let's Encrypt)
- WSS (WebSocket Secure) для OCPP
- Certificate pinning (рекомендуется для мобильных)

### 9. **Sensitive Data**

- API ключи: Environment variables (Docker secrets)
- JWT Secret: Environment variable
- Payment data: Логирование с маскировкой
- OBANK сертификаты: mounted volumes (read-only)

### 10. **RLS (Row Level Security)** в Supabase

```sql
-- Клиенты видят только свои данные
ALTER TABLE charging_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users see own sessions" ON charging_sessions
  FOR SELECT USING (client_id = current_user_id());
```

---

## Развертывание

### 1. **Docker контейнеризация**

**Multi-stage Dockerfile (production):**

```dockerfile
Stage 1: Builder
├── Python 3.11-slim
├── Компилирование Python пакетов
└── Virtual Environment: /opt/venv

Stage 2: Runtime
├── Python 3.11-slim (минимальный)
├── Non-root user: evpower (uid 1000)
├── dumb-init для правильного shutdown
└── Health check (curl на /health)

Результат:
- ~400MB базовый образ
- Security: non-root пользователь
- Signal handling: dumb-init для graceful shutdown
```

### 2. **Docker Compose**

**Development** (`docker-compose.yml`):
```yaml
Services:
├── redis:alpine        - Кэширование (256MB max-memory)
└── evpower-backend     - FastAPI сервер (port 9210)

Volumes:
└── redis-data          - Персистенция Redis
```

**Production** (`docker-compose.production.yml`):
```yaml
Services:
├── redis:alpine        - Кэширование
└── evpower-backend     - FastAPI сервер

Networks:
└── coolify             - External Docker network (Traefik)

Traefik Labels:
├── Routing: Host(ocpp.evpower.kg)
├── TLS: Let's Encrypt auto-renewal
├── WebSocket support: X-Forwarded-Proto headers
└── Dual stack: HTTPS + HTTP (для ws://)
```

### 3. **Переменные окружения**

**Обязательные:**
```bash
# Database
DATABASE_URL=postgresql://user:pass@host:5432/db

# Supabase
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=xxx
SUPABASE_SERVICE_ROLE_KEY=xxx
SUPABASE_JWT_SECRET=xxx

# Redis
REDIS_URL=redis://redis:6379/0

# Payment Provider
PAYMENT_PROVIDER=ODENGI  # или OBANK
ODENGI_MERCHANT_ID=xxx
ODENGI_PASSWORD=xxx
```

**Опциональные:**
```bash
# CORS
CORS_ORIGINS=https://app.example.com,https://web.example.com

# Rate Limiting
RATE_LIMIT_DEFAULT_PER_MINUTE=60
RATE_LIMIT_CRITICAL_PER_MINUTE=10
RATE_LIMIT_WEBHOOK_PER_MINUTE=30

# Logging
LOG_LEVEL=INFO
ENABLE_SWAGGER=false  # Только для development
```

### 4. **Health Checks**

```yaml
/health        - Проверка Redis + базовое здоровье
/readyz        - Готовность зависимостей
Docker HEALTHCHECK - curl на /health (30s interval)
```

### 5. **Graceful Shutdown**

```python
lifespan(app: FastAPI):
    yield  # Application running
    
    # На SIGTERM:
    scheduler.shutdown()
    payment_cleanup_task_ref.cancel()
    redis.close()
```

Время ожидания: 30 секунд (Kubernetes default)

---

## Ключевые технологические решения

### Почему FastAPI?
- ✅ Асинхронность out-of-the-box
- ✅ Автоматическая документация (OpenAPI)
- ✅ Быстрая валидация Pydantic
- ✅ WebSocket поддержка
- ✅ Зрелый фреймворк (production-ready)

### Почему WebSocket OCPP?
- ✅ Стандарт для EV charging
- ✅ Двусторонняя коммуникация
- ✅ Низкая латентность
- ✅ Поддержка команд от сервера к станции

### Почему Redis?
- ✅ In-memory кэш для станций
- ✅ Pub/Sub для команд
- ✅ Session хранилище
- ✅ Очень быстро

### Почему Supabase PostgreSQL?
- ✅ Managed PostgreSQL
- ✅ RLS для безопасности
- ✅ Real-time подписки
- ✅ Встроенная аутентификация

---

## Версия v1.2.2 - Критические исправления

**Дата:** 2025-11-02

### 🔥 Исправления:

1. **Race condition в платежах**
   - Проблема: Cleanup task отменял платеж перед приходом webhook
   - Решение: Removed status check, approve webhook обрабатывается независимо
   - Impact: 🔴 КРИТИЧЕСКИЙ

2. **Таймаут подключения кабеля**
   - Проблема: Средства блокировались на 12 часов без подключения
   - Решение: Добавлена auto-stop через 10 минут без OCPP транзакции
   - Impact: 🔴 КРИТИЧЕСКИЙ

3. **Синхронизация таймаута invoice**
   - Проблема: QR код 5 мин, invoice 10 мин (противоречие)
   - Решение: Унифицировано на 5 минут везде
   - Impact: 🟡 СРЕДНИЙ

---

## Рекомендации для развития

### Краткосрочные (1-2 месяца):
1. Добавить метрики Prometheus (запросы, задержки, ошибки)
2. Интегрировать Sentry для отслеживания ошибок
3. Добавить WebSocket reconnection logic в мобильное приложение

### Среднесрочные (3-6 месяцев):
1. Миграция на PostgreSQL async (asyncpg вместо sync SQLAlchemy)
2. Кластеризация Redis (Redis Cluster)
3. Горизонтальное масштабирование (несколько инстансов + load balancer)

### Долгосрочные (6-12 месяцев):
1. Support для OCPP 2.0 (более современный)
2. Machine learning для предсказания спроса на зарядку
3. Интеграция с системой ERP (инвентаризация, техническое обслуживание)

