#!/bin/bash

echo "🚀 Starting EvPower OCPP Server..."
echo "📍 Domain: https://ocpp.evpower.kg"
echo "🌐 HTTP API: Port 9210"
echo "⚡ WebSocket: Port 80 (OCPP standard)"

# Уже в правильной директории /app после COPY backend/ .
echo "Working directory: $(pwd)"
echo "Contents: $(ls -la)"

# Создать директорию для логов если приложение все еще пытается их использовать
mkdir -p logs

# Функция для обработки сигналов
cleanup() {
    echo "🛑 Получен сигнал остановки. Завершаем все процессы..."
    # Убиваем все дочерние процессы
    kill -TERM "$FASTAPI_PID" "$OCPP_PID" 2>/dev/null
    wait "$FASTAPI_PID" "$OCPP_PID" 2>/dev/null
    echo "✅ Все процессы завершены"
    exit 0
}

# Обработка сигналов для корректной остановки
trap cleanup SIGTERM SIGINT

# Запуск FastAPI на порту 9210
echo "🌐 Запуск FastAPI на порту 9210..."
python -m uvicorn app.main:app --host 0.0.0.0 --port 9210 &
FASTAPI_PID=$!

# Запуск OCPP WebSocket сервера на порту 80
echo "⚡ Запуск OCPP WebSocket сервера на порту 80..."
OCPP_WS_PORT=80 python -m ocpp_ws_server.server &
OCPP_PID=$!

echo "✅ Оба сервиса запущены:"
echo "   - FastAPI PID: $FASTAPI_PID (порт 9210)"
echo "   - OCPP WS PID: $OCPP_PID (порт 80)"

# Ожидание завершения любого из процессов
wait -n $FASTAPI_PID $OCPP_PID

# Если один процесс завершился, останавливаем второй
echo "⚠️  Один из процессов завершился. Останавливаем все..."
cleanup 