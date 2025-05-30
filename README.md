# EvPower Backend

Минимальный OCPP 1.6 WebSocket сервер для зарядных станций + Mobile API.

## Установка

```bash
cd backend
pip install -r requirements.txt
```

## Запуск

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Эндпоинты

- **WS** `/ws/{station_id}` - OCPP 1.6 подключение станций
- **POST** `/api/mobile/charging/start` - запуск зарядки  
- **POST** `/api/mobile/charging/stop` - остановка зарядки
- **POST** `/api/mobile/charging/status` - статус зарядки
- **POST** `/api/mobile/station/status` - статус станции
- **GET** `/health` - health check 