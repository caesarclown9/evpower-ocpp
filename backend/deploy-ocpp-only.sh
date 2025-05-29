#!/bin/bash

# Безопасный деплой ТОЛЬКО OCPP сервера БЕЗ nginx
# Запускать на сервере под пользователем с sudo привилегиями
# ⚠️ НЕ ПЕРЕЗАПИСЫВАЕТ .env ФАЙЛ! ⚠️

set -e

echo "🚀 Деплой чистого OCPP сервера (БЕЗ nginx)..."

# Переменные
PROJECT_DIR="/opt/evpower-ocpp"
BACKEND_DIR="$PROJECT_DIR/backend"
SERVICE_NAME="evpower-ocpp"

# Создание бэкапа текущего .env
if [ -f "$BACKEND_DIR/.env" ]; then
    cp "$BACKEND_DIR/.env" "$BACKEND_DIR/.env.backup.$(date +%Y%m%d_%H%M%S)"
    echo "✅ Создан бэкап .env файла"
fi

# Остановка сервиса
echo "⏹️ Остановка OCPP сервиса..."
sudo systemctl stop $SERVICE_NAME || true

# Остановка nginx если он мешает
echo "🚫 Отключение nginx от порта 8180..."
sudo rm -f /etc/nginx/sites-enabled/evpower-ocpp
sudo systemctl reload nginx || true

# Обновление кода из GitHub
echo "📥 Получение обновлений из GitHub..."
cd $PROJECT_DIR
git pull origin main

# Активация виртуального окружения
echo "🐍 Активация виртуального окружения..."
cd $BACKEND_DIR
source venv/bin/activate

# Обновление зависимостей
echo "📦 Обновление зависимостей..."
pip install -r requirements.txt

# Обновление systemd сервиса
echo "⚙️ Обновление systemd сервиса..."
if [ -f "ocpp-server.service" ]; then
    sudo cp ocpp-server.service /etc/systemd/system/$SERVICE_NAME.service
    sudo systemctl daemon-reload
    echo "✅ Systemd сервис обновлен"
fi

# ⚠️ БЕЗОПАСНОСТЬ: НЕ ПЕРЕЗАПИСЫВАЕМ .env!
echo "🛡️ Пропускаем обновление .env для сохранения рабочих настроек"
if [ -f "env.production.template" ]; then
    echo "ℹ️  Шаблон настроек доступен в env.production.template"
    echo "ℹ️  Создайте .env на основе шаблона если нужно"
    echo "🔐 ВАЖНО: НЕ добавляйте реальные секреты в Git!"
fi

# Создание директории логов
echo "📁 Создание директории логов..."
sudo mkdir -p /var/log/evpower-ocpp
sudo chown evpower:evpower /var/log/evpower-ocpp

# Проверка что порт 8180 свободен
echo "🔍 Проверка порта 8180..."
if sudo netstat -tlnp | grep :8180 | grep -v $SERVICE_NAME; then
    echo "❌ Порт 8180 занят другим процессом!"
    echo "📋 Запущенные процессы на порту 8180:"
    sudo lsof -i :8180
    echo "🛠️ Остановите мешающие процессы и повторите деплой"
    exit 1
fi

# Запуск миграций базы данных
echo "🗄️ Выполнение миграций базы данных..."
python -c "
from app.db.base_class import Base
from app.db.session import engine
Base.metadata.create_all(bind=engine)
print('База данных инициализирована')
"

# Включение и запуск сервиса
echo "🚀 Запуск OCPP сервиса..."
sudo systemctl enable $SERVICE_NAME
sudo systemctl start $SERVICE_NAME

# Проверка статуса
echo "📊 Проверка статуса сервиса..."
sudo systemctl status $SERVICE_NAME --no-pager

# Ожидание запуска
echo "⏳ Ожидание запуска сервера..."
sleep 10

# Проверка health check
echo "🏥 Проверка работоспособности..."
if curl -f -s http://localhost:8180/health > /dev/null; then
    echo "✅ OCPP сервер успешно запущен на порту 8180"
    echo "🌐 API доступно: http://193.176.239.218:8180"
    echo "📚 Документация: http://193.176.239.218:8180/docs"
    echo "🔌 WebSocket: ws://193.176.239.218:8180/ws/{station_id}"
    echo "📊 Статистика: http://193.176.239.218:8180/api/v1/ocpp/statistics/overview"
else
    echo "❌ Ошибка: сервер не отвечает на health check"
    echo "📋 Проверьте логи: sudo journalctl -u $SERVICE_NAME -f"
    exit 1
fi

echo "🎉 Деплой чистого OCPP сервера завершен успешно!"
echo "🛡️ Файл .env НЕ БЫЛ ИЗМЕНЕН"
echo "🚫 Nginx отключен от порта 8180"
echo "⚡ Только FastAPI на 8180 для OCPP функций" 