#!/bin/bash

echo "🚀 Starting EvPower OCPP Server..."
echo "📍 Domain: https://ocpp.evpower.kg"
echo "🌐 HTTP API + WebSocket: Port 9210"

# Уже в правильной директории /app после COPY backend/ .
echo "Working directory: $(pwd)"
echo "Contents: $(ls -la)"

# Создать директорию для логов если приложение все еще пытается их использовать
mkdir -p logs

# Запустить приложение на порту 9210 (API и WebSocket на одном порту)
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 9210