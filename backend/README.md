# OCPP WebSocket Server Backend

## Описание
Бэкенд реализует OCPP 1.6 WebSocket сервер для управления электрозарядными станциями. Поддерживает протокол OCPP с управлением сессиями зарядки, тарификацией и мониторингом станций через Redis.

🔌 **WebSocket URL:** `ws://your-domain.com/ws/{station_id}`

---

## Установка и запуск (Windows/PowerShell)

### 1. Клонируйте репозиторий и перейдите в папку backend
```powershell
git clone https://github.com/caesarclown9/evpower-ocpp.git
cd evpower-ocpp\backend
```

### 2. Установите зависимости
```powershell
pip install -r requirements.txt
```

### 3. Настройте переменные окружения
Создайте файл `.env` в папке backend со следующим содержимым:
```env
DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>
REDIS_URL=redis://localhost:6379/0
APP_HOST=0.0.0.0
APP_PORT=8000
```

### 4. Запуск OCPP WebSocket сервера (FastAPI)
```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Запуск отдельного OCPP WebSocket сервера (опционально)
```powershell
python ocpp_ws_server/server.py
```

---

## API Endpoints

### OCPP Management
- `GET /ocpp/connections` - Список подключенных станций
- `POST /ocpp/send_command` - Отправка команд на станции
- `GET /ocpp/status/{station_id}` - Статус конкретной станции

### Тарифы
- `POST /ocpp/tariffs` - Создание тарифа
- `GET /ocpp/tariffs` - Список тарифов
- `DELETE /ocpp/tariffs/{tariff_id}` - Удаление тарифа

### Сессии зарядки
- `POST /ocpp/sessions` - Создание сессии
- `GET /ocpp/sessions` - Список сессий
- `POST /ocpp/start_charge` - Запуск зарядки

### WebSocket
- `ws://localhost:8000/ws/{station_id}` - OCPP 1.6 WebSocket endpoint

---

## Swagger/OpenAPI
Документация будет доступна по адресу:  
`http://localhost:8000/docs`

---

## Production деплой

Для production развертывания на сервере смотрите [PRODUCTION_SETUP.md](PRODUCTION_SETUP.md)

**Быстрый старт:**
```bash
# На Ubuntu сервере
wget https://raw.githubusercontent.com/caesarclown9/evpower-ocpp/main/backend/server-setup.sh
chmod +x server-setup.sh
sudo ./server-setup.sh
```

---

## Архитектура

- **FastAPI** - REST API для управления
- **OCPP 1.6** - Протокол связи с зарядными станциями (ws://)
- **Redis** - Pub/Sub для команд и хранение состояний
- **PostgreSQL** - Хранение тарифов и сессий зарядки
- **WebSocket** - Связь с зарядными станциями
- **Nginx** - Reverse proxy (для production)

---

## Тестирование

### Подключение тестовой станции
```powershell
# Используйте OCPP клиент
python ocpp_ws_server/client.py --chargebox_id TEST-STATION-001 --ocpp_url ws://localhost:8000/ws/TEST-STATION-001
```

### Проверка health check
```bash
curl http://localhost:8000/health
```

---

## Настройка реальных станций

Для подключения настоящих зарядных станций:

1. **Central System URL:** `ws://your-domain.com/ws/`
2. **Charge Point ID:** Уникальный ID станции
3. **Protocol:** OCPP 1.6
4. **Subprotocol:** `ocpp1.6`

**Примеры URL:**
- `ws://your-server-ip/ws/STATION-001`
- `ws://your-domain.com/ws/BERLIN-STATION-05`

---

## Production endpoints

После деплоя на сервер:
- 🔌 **WebSocket:** `ws://your-domain.com/ws/{station_id}`
- 📚 **API Docs:** `http://your-domain.com/docs`
- ❤️ **Health Check:** `http://your-domain.com/health`

---

## TODO:
- Реализовать все модули API
- Добавить docker-compose.yml
- Добавить тесты
- Описать RBAC для ролей client, operator, admin, superadmin 