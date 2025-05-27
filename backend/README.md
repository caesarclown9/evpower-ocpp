# EvPower OCPP Backend

🔌 **Production-ready OCPP 1.6 WebSocket сервер** для управления зарядными станциями электромобилей с интеграцией **Supabase**.

## 🚀 Возможности

- ✅ **OCPP 1.6** полная поддержка протокола
- ✅ **WebSocket** real-time связь со станциями  
- ✅ **Supabase** облачная PostgreSQL база данных
- ✅ **Redis** pub/sub для команд станциям
- ✅ **FastAPI** REST API с автодокументацией
- ✅ **Тарификация** гибкая система расчета стоимости
- ✅ **Мониторинг** health checks и логирование
- ✅ **Production** готово к развертыванию

## 🏗️ Архитектура

### База данных (Supabase)
```
users (5 записей)           ← Пользователи системы
├── charging_sessions        ← Сессии зарядки (с транзакциями OCPP)
clients                      ← Клиенты/компании
locations (17 записей)       ← Локации станций
├── stations (22 записи)     ← Зарядные станции
    ├── maintenance          ← Техобслуживание
    ├── charging_sessions    ← Сессии зарядки
    └── tariff_plans         ← Тарифные планы
        └── tariff_rules     ← Правила тарификации (18 записей)
```

### OCPP Workflow
```
Station → WebSocket → OCPP Handler → Database → Response
                           ↓
                      Redis Pub/Sub ← API Commands
```

## 📋 Предварительные требования

- Python 3.9+
- Redis Server
- Доступ к интернету (для Supabase)

## 🛠️ Установка

1. **Клонирование репозитория:**
```bash
git clone https://github.com/caesarclown9/evpower-ocpp.git
cd evpower-ocpp/backend
```

2. **Установка зависимостей:**
```bash
pip install -r requirements.txt
```

3. **Настройка переменных окружения:**
```bash
cp env.example .env
```

Отредактируйте `.env` файл с вашими настройками:
```env
# Database settings for Supabase
DATABASE_URL=postgresql://postgres.fsoffzrngojgsigrmlui:Arma2000@aws-0-eu-north-1.pooler.supabase.com:6543/postgres
SUPABASE_URL=https://fsoffzrngojgsigrmlui.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Redis settings  
REDIS_URL=redis://localhost:6379

# Server settings
LOG_LEVEL=INFO
ALLOWED_HOSTS=*
```

4. **Запуск Redis:**
```bash
redis-server
```

5. **Создание таблиц в базе данных:**
```bash
python test_db.py
```

## 🚀 Запуск

### Development режим:
```bash
python app/main.py
```

### Production режим:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

Сервер будет доступен по адресу:
- **API документация:** http://localhost:8000/docs
- **Health check:** http://localhost:8000/health
- **OCPP WebSocket:** ws://localhost:8000/ws/{station_id}

## 📡 OCPP Подключение

### Подключение станции:
```
WebSocket URL: ws://your-server.com/ws/STATION_ID
Subprotocol: ocpp1.6
```

### Поддерживаемые OCPP сообщения:
- ✅ **BootNotification** - регистрация станции
- ✅ **Heartbeat** - проверка связи (каждые 5 минут)
- ✅ **StartTransaction** - начало зарядки
- ✅ **StopTransaction** - завершение зарядки  
- ✅ **MeterValues** - показания счетчика
- ✅ **RemoteStartTransaction** - дистанционный запуск
- ✅ **RemoteStopTransaction** - дистанционная остановка

## 🛠️ API Эндпоинты

### OCPP Управление
```http
GET    /ocpp/connections              # Список подключенных станций
GET    /ocpp/status/{station_id}      # Статус станции
POST   /ocpp/send_command             # Отправка команды на станцию
POST   /ocpp/start_charge             # Запуск зарядки
```

### Управление данными
```http
# Пользователи
POST   /ocpp/users                    # Создание пользователя
GET    /ocpp/users                    # Список пользователей
GET    /ocpp/users/{user_id}          # Данные пользователя

# Станции  
POST   /ocpp/stations                 # Создание станции
GET    /ocpp/stations                 # Список станций
GET    /ocpp/stations/{station_id}    # Данные станции
PUT    /ocpp/stations/{station_id}    # Обновление станции

# Сессии зарядки
POST   /ocpp/sessions                 # Создание сессии
GET    /ocpp/sessions                 # Список сессий
GET    /ocpp/sessions/{session_id}    # Данные сессии

# Тарифы
POST   /ocpp/tariff_plans             # Создание тарифного плана
GET    /ocpp/tariff_plans             # Список тарифных планов
POST   /ocpp/tariff_rules             # Создание тарифного правила
GET    /ocpp/calculate_cost/{station_id}?energy_kwh=25.5  # Расчет стоимости
```

## 💰 Тарификация

Система поддерживает гибкую тарификацию:

### Типы тарифов:
- **per_kwh** - за киловатт-час (15.0 KGS/кВт⋅ч)
- **per_minute** - за минуту зарядки
- **session_fee** - фиксированная плата за сессию
- **parking_fee** - плата за парковку

### Пример расчета:
```python
# 25.5 кВт⋅ч × 15.0 KGS/кВт⋅ч = 382.5 KGS
GET /ocpp/calculate_cost/station_id?energy_kwh=25.5
```

## 🔧 Тестирование

### Тест подключения к базе:
```bash
python test_db.py
```

### Полный тест OCPP операций:
```bash
python test_ocpp_crud.py
```

### Пример успешного вывода:
```
🧪 Тестирование OCPP операций с Supabase...

1. Создание пользователя...
✅ Пользователь создан: test_1748379539@evpower.kg

2. Создание локации... 
✅ Локация создана: 43cb1ce0-15d0-4911-b339-5b0c3fd41281

5. Создание станции...
✅ Станция создана: TEST-1748379539

7. Расчет стоимости зарядки...
✅ Стоимость зарядки 25.5 кВт*ч: {'cost': 382.5, 'currency': 'KGS'}

🎉 Все операции OCPP успешно выполнены с базой данных Supabase!
```

## 🌐 Production Развертывание

Проект готов для развертывания на сервере:

1. **Настройка Nginx** (HTTP-only для совместимости со станциями):
```nginx
server {
    listen 80;
    server_name your-server.com;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

2. **Systemd сервис:**
```bash
sudo cp ocpp-server.service /etc/systemd/system/
sudo systemctl enable ocpp-server
sudo systemctl start ocpp-server
```

3. **Мониторинг:**
```bash
# Проверка здоровья сервиса
curl http://your-server.com/health

# Логи сервиса
sudo journalctl -u ocpp-server -f
```

## 📊 База данных

### Статистика (текущее состояние):
- **Пользователи:** 5 записей
- **Локации:** 17 записей  
- **Станции:** 22 записи
- **Тарифные планы:** 6 записей
- **Тарифные правила:** 18 записей
- **Сессии зарядки:** активно используется

### Производительность:
- ✅ Индексы созданы для всех критичных запросов
- ✅ Connection pooling настроен
- ✅ Оптимизация для real-time OCPP операций

## 🛡️ Безопасность

- ✅ Валидация входных данных через Pydantic
- ✅ SQL injection защита через SQLAlchemy ORM
- ✅ CORS настройки для production
- ✅ Логирование всех операций
- ✅ Health checks для мониторинга

## 📝 Логирование

Все OCPP операции логируются:
```
2024-01-28 12:00:00 - ChargePoint.TEST-001 - INFO - 🔌 BootNotification: AC-22kW, EVPower
2024-01-28 12:05:00 - ChargePoint.TEST-001 - INFO - ▶️ StartTransaction: connector=1, meter_start=1000
2024-01-28 12:30:00 - ChargePoint.TEST-001 - INFO - ⏹️ StopTransaction: energy=25.5kWh, amount=382.5 KGS
```

## 🔗 Полезные ссылки

- [OCPP 1.6 Specification](https://www.openchargealliance.org/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Supabase Documentation](https://supabase.com/docs)
- [Redis Documentation](https://redis.io/documentation)

## 📞 Поддержка

При возникновении вопросов:
1. Проверьте логи: `sudo journalctl -u ocpp-server`
2. Убедитесь что Redis запущен: `redis-cli ping`
3. Проверьте health check: `curl http://localhost:8000/health`

## 📄 Лицензия

MIT License - см. файл LICENSE для деталей.

---

**Made with ❤️ for EV charging infrastructure** 