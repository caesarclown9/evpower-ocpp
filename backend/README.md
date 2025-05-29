# EvPower OCPP WebSocket Server

**Минимальный OCPP 1.6 WebSocket сервер для зарядных станций электромобилей**

## 🎯 Назначение

Этот сервер предоставляет **только WebSocket подключения** для зарядных станций по протоколу OCPP 1.6. 

**HTTP API endpoints реализованы отдельно в FlutterFlow проекте** для мобильного приложения и веб-интерфейса.

## 🔌 Endpoints

### **WebSocket**
```
ws://193.176.239.218:8180/ws/{station_id}
```
- Основной endpoint для подключения ЭЗС
- Поддерживает OCPP 1.6 JSON протокол

### **HTTP (минимальные)**
```
GET /health - проверка здоровья сервера
GET /      - информация о сервере
```

## 📋 Поддерживаемые OCPP сообщения

### **От станции к серверу (Call)**
- `BootNotification` - регистрация станции при запуске
- `Heartbeat` - периодические сигналы жизни 
- `StatusNotification` - изменения статуса коннекторов
- `Authorize` - авторизация RFID карт
- `StartTransaction` - начало сеанса зарядки
- `StopTransaction` - завершение сеанса зарядки
- `MeterValues` - показания счетчиков энергии

### **От сервера к станции (Call)**
- `RemoteStartTransaction` - удаленный запуск зарядки
- `RemoteStopTransaction` - удаленная остановка зарядки
- `ChangeConfiguration` - изменение настроек станции
- `GetConfiguration` - получение настроек станции

## 🗄️ База данных (Supabase)

### **OCPP таблицы:**
- `ocpp_station_status` - статусы станций и heartbeat
- `ocpp_transactions` - транзакции зарядки  
- `ocpp_meter_values` - показания счетчиков
- `ocpp_authorization` - авторизованные RFID карты
- `ocpp_configuration` - конфигурации станций

### **Основные таблицы:**
- `stations` - информация о станциях
- `users` - пользователи системы
- `charging_sessions` - сеансы зарядки
- `locations` - локации станций

## 🚀 Развертывание

### **Production сервер:**
- **Host:** 193.176.239.218:8180
- **Протокол:** OCPP 1.6 JSON over WebSocket
- **Reverse Proxy:** nginx
- **Systemd Service:** evpower-ocpp.service

### **Структура deployment:**
```
FastAPI (0.0.0.0:8000) ← nginx (0.0.0.0:8180) ← Интернет
```

## 🔧 Настройка

### **Environment переменные (.env):**
```bash
SUPABASE_URL=https://fsoffzrngojgsigrmlui.supabase.co
SUPABASE_KEY=your_anon_key
REDIS_URL=redis://localhost:6379
ALLOWED_HOSTS=193.176.239.218,localhost
LOG_LEVEL=INFO
```

### **Конфигурация Redis:**
- **Каналы:** `ocpp_commands_{station_id}` для удаленных команд
- **Ключи:** `ocpp_stations` для списка подключенных станций

## 📊 Мониторинг

### **Health Check:**
```bash
curl http://193.176.239.218:8180/health
```

### **Логи сервиса:**
```bash
sudo journalctl -u evpower-ocpp.service -f
```

### **Подключенные станции:**
```bash
redis-cli SMEMBERS ocpp_stations
```

## 🔍 Troubleshooting

### **Проблемы подключения WebSocket:**
1. Проверить nginx конфигурацию
2. Проверить firewall (порт 8180)
3. Проверить статус сервиса

### **Проблемы с базой данных:**
1. Проверить Supabase URL и ключи
2. Проверить foreign key constraints
3. Проверить права доступа

### **Проблемы с Redis:**
1. Проверить Redis сервис
2. Проверить подключение к localhost:6379
3. Проверить память Redis

## 📁 Файловая структура

```
backend/
├── app/
│   ├── main.py              # FastAPI приложение (минимальное)
│   ├── core/config.py       # Конфигурация
│   ├── db/                  # База данных
│   │   ├── models/ocpp.py   # OCPP модели
│   │   └── session.py       # Подключение к БД
│   └── crud/ocpp_service.py # OCPP бизнес-логика
├── ocpp_ws_server/          # WebSocket сервер
│   ├── ws_handler.py        # Обработчик WebSocket
│   ├── ocpp_dispatcher.py   # OCPP диспетчер
│   └── redis_manager.py     # Redis менеджер
└── requirements.txt         # Зависимости
```

## 🔗 Архитектурные решения

### **Разделение ответственности:**
- **Backend (этот проект):** Только OCPP WebSocket протокол для ЭЗС
- **FlutterFlow:** HTTP API + мобильное приложение + веб-интерфейс
- **Supabase:** База данных + Auth + Real-time
- **Redis:** Кеширование + pub/sub для команд

### **Преимущества:**
- ✅ Четкое разделение backend/frontend
- ✅ Независимое развитие проектов  
- ✅ Специализация на OCPP протоколе
- ✅ Простота обслуживания и масштабирования

## 📞 Контакты

- **Проект:** EvPower OCPP Server
- **Версия:** 1.0.0
- **Сервер:** 193.176.239.218:8180

**Готов к production использованию! 🚀** 