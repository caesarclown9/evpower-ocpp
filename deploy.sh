#!/bin/bash

# Простой деплой скрипт для EvPower Backend

echo "🚀 Deploying EvPower Backend..."

# Создание .env файла если не существует
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Please create it based on backend/env.example"
    echo "Copy backend/env.example to .env and update values"
    exit 1
fi

# Остановка предыдущих контейнеров
echo "🛑 Stopping existing containers..."
docker-compose down

# Сборка и запуск
echo "🔨 Building and starting containers..."
docker-compose up -d --build

# Проверка статуса
echo "📊 Checking status..."
docker-compose ps

echo "✅ Deployment complete!"
echo "🌐 FastAPI: http://localhost:8000"
echo "🔌 WebSocket: ws://localhost:8180" 