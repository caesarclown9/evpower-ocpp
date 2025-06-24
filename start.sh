#!/bin/bash

echo "🚀 Starting EvPower OCPP Server..."
echo "📍 Domain: https://ocpp.evpower.kg"
echo "🌐 HTTP API: Port 8180"
echo "⚡ WebSocket: Port 8180 (same as HTTP)"

# Перейти в папку backend
cd /app/backend 2>/dev/null || cd backend 2>/dev/null || echo "Already in correct directory"

# Запустить приложение на порту 8180
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8180 