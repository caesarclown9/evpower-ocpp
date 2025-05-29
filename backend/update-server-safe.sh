#!/bin/bash

# БЕЗОПАСНЫЙ скрипт обновления EvPower OCPP сервера
# Запускать на сервере под пользователем с sudo привилегиями
# ⚠️ НЕ ПЕРЕЗАПИСЫВАЕТ .env ФАЙЛ! ⚠️

set -e

echo "🔄 Безопасное обновление EvPower OCPP сервера..."

# Переменные
PROJECT_DIR="/opt/evpower-ocpp"
BACKEND_DIR="$PROJECT_DIR/backend"
SERVICE_NAME="evpower-ocpp"
NGINX_SITE="evpower-ocpp"

# Создание бэкапа текущего .env
if [ -f "$BACKEND_DIR/.env" ]; then
    cp "$BACKEND_DIR/.env" "$BACKEND_DIR/.env.backup.$(date +%Y%m%d_%H%M%S)"
    echo "✅ Создан бэкап .env файла"
fi

# Остановка сервиса
echo "⏹️ Остановка сервиса..."
sudo systemctl stop $SERVICE_NAME || true

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

# Копирование конфигураций
echo "⚙️ Обновление конфигураций..."

# Обновление systemd сервиса
if [ -f "ocpp-server.service" ]; then
    sudo cp ocpp-server.service /etc/systemd/system/$SERVICE_NAME.service
    sudo systemctl daemon-reload
    echo "✅ Systemd сервис обновлен"
fi

# Обновление nginx конфигурации
if [ -f "nginx.conf" ]; then
    sudo cp nginx.conf /etc/nginx/sites-available/$NGINX_SITE
    sudo ln -sf /etc/nginx/sites-available/$NGINX_SITE /etc/nginx/sites-enabled/
    sudo nginx -t && sudo systemctl reload nginx
    echo "✅ Nginx конфигурация обновлена"
fi

# ⚠️ БЕЗОПАСНОСТЬ: НЕ ПЕРЕЗАПИСЫВАЕМ .env!
echo "🛡️ Пропускаем обновление .env для сохранения рабочих настроек"
if [ -f "env.production.template" ]; then
    echo "ℹ️  Шаблон настроек доступен в env.production.template"
    echo "ℹ️  Создайте env.production на основе шаблона если нужно"
    echo "🔐 ВАЖНО: НЕ добавляйте реальные секреты в Git!"
fi

# Создание директории логов
sudo mkdir -p /var/log/evpower-ocpp
sudo chown evpower:evpower /var/log/evpower-ocpp

# Запуск миграций базы данных
echo "🗄️ Выполнение миграций базы данных..."
python -c "
from app.db.base_class import Base
from app.db.session import engine
Base.metadata.create_all(bind=engine)
print('База данных инициализирована')
"

# Включение и запуск сервиса
echo "🚀 Запуск сервиса..."
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
    echo "✅ Сервер успешно запущен и доступен на порту 8180"
    echo "🌐 API доступно по адресу: http://193.176.239.218:8180"
    echo "📚 Документация: http://193.176.239.218:8180/docs"
else
    echo "❌ Ошибка: сервер не отвечает на health check"
    echo "📋 Проверьте логи: sudo journalctl -u $SERVICE_NAME -f"
    exit 1
fi

echo "🎉 Безопасное обновление завершено успешно!"
echo "🛡️ Файл .env НЕ БЫЛ ИЗМЕНЕН"
echo "📋 Бэкап создан: .env.backup.$(date +%Y%m%d_%H%M%S)" 