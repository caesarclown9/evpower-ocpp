# OCPP WebSocket Server & API

## Назначение

Модуль предназначен для управления зарядными станциями по протоколу OCPP 1.6 через WebSocket и интеграции с backend API (FastAPI). Поддерживает реальные бизнес-сценарии: запуск/остановка зарядки по лимиту, учёт баланса, тарификация, контроль сессий и транзакций.

---

## Архитектура
- **ocpp_ws_server/server.py** — OCPP 1.6 WebSocket сервер (python-ocpp)
- **ocpp_ws_server/redis_manager.py** — асинхронный менеджер Redis (Pub/Sub, хранение статусов)
- **app/api/ocpp.py** — FastAPI-роуты для управления станциями, сессиями, тарифами
- **app/db/models/ocpp.py** — модели ChargingSession, Tariff
- **app/crud/ocpp.py** — CRUD для сессий и тарифов
- **app/db/models/user.py** — модель пользователя с балансом

---

## Запуск (Windows/PowerShell)

```powershell
# 1. Установить зависимости
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

# 2. Запустить Redis (локально или через Docker)
# Пример для Docker:
docker run -d -p 6379:6379 --name redis redis

# 3. Запустить OCPP WebSocket сервер
python .\ocpp_ws_server\server.py

# 4. Запустить FastAPI backend (отдельно)
# (например, uvicorn app.main:app --reload)
```

---

## Бизнес-сценарий (end-to-end)
1. Пользователь регистрируется, пополняет баланс
2. Выбирает станцию и сумму/энергию для зарядки
3. Через API (`/ocpp/start_charge`) создаётся ChargingSession, рассчитывается лимит, баланс проверяется
4. На станцию отправляется команда RemoteStartTransaction с лимитом
5. Станция начинает зарядку, отправляет MeterValues
6. При достижении лимита или по команде вызывается StopTransaction
7. Сессия фиксируется, средства списываются, статус обновляется

---

## Примеры API-запросов (Swagger)

### Запуск зарядки с лимитом
```http
POST /ocpp/start_charge
{
  "station_id": "DE-BERLIN-001",
  "user_id": "...",
  "limit_type": "amount",  // или "energy"
  "limit_value": 500        // сумма в KGS или энергия в кВт*ч
}
```

### CRUD тарифов
```http
POST /ocpp/tariffs
{
  "station_id": "DE-BERLIN-001",
  "price_per_kwh": 12.5,
  "currency": "KGS"
}
```

### Получить сессии пользователя
```http
GET /ocpp/sessions?user_id=...
```

---

## Тестирование с эмулятором станции
- Подключить эмулятор к ws://localhost:8180/ws/{station_id}
- Отправить BootNotification, StartTransaction, MeterValues, StopTransaction
- Проверить, что лимиты и баланс учитываются, сессии фиксируются

---

## FAQ
- **Q:** Как работает лимит?  
  **A:** Лимит по сумме/энергии рассчитывается на backend, контролируется через MeterValues. При достижении лимита зарядка останавливается автоматически.
- **Q:** Что если не хватает средств?  
  **A:** Сессия помечается как error, средства не списываются.
- **Q:** Можно ли интегрировать с реальной станцией?  
  **A:** Да, сервер полностью совместим с OCPP 1.6 и поддерживает реальные сценарии.

---

## TODO
- Интеграция с платёжными системами
- Расширение логики MeterValues (учёт времени, мощности)
- Мониторинг и алерты 