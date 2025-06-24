#!/bin/bash

echo "🚀 Starting EvPower OCPP Server..."
echo "📍 Domain: https://ocpp.evpower.kg"
echo "🌐 HTTP API: Port 8180"
echo "⚡ WebSocket: Port 8180 (same as HTTP)"

# Уже в правильной директории /app после COPY backend/ .
echo "Working directory: $(pwd)"
echo "Contents: $(ls -la)"

# Создать директорию для логов если приложение все еще пытается их использовать
mkdir -p logs

# Запустить приложение на порту 8180
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8180 