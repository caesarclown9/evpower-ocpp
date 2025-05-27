#!/bin/bash

# Скрипт деплоя OCPP сервера на production (HTTP версия)
set -e

echo "🚀 Начинаем деплой OCPP сервера (HTTP)..."

# Переменные (настройте под ваш сервер)
DOMAIN="your-domain.com"
USER="ocpp"
APP_DIR="/home/ocpp/EvPower-Backend/backend"
REPO_URL="https://github.com/caesarclown9/evpower-ocpp.git"

# Функция логирования
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# 1. Создание пользователя и директорий
log "👤 Настройка пользователя и директорий..."
sudo mkdir -p /var/log/ocpp
sudo chown $USER:$USER /var/log/ocpp
sudo chmod 755 /var/log/ocpp

# 2. Клонирование или обновление репозитория
if [ -d "$APP_DIR" ]; then
    log "📥 Обновление существующего репозитория..."
    cd $APP_DIR
    git pull origin main
else
    log "📥 Клонирование репозитория..."
    sudo -u $USER git clone $REPO_URL /home/ocpp/EvPower-Backend
    cd $APP_DIR
fi

# 3. Настройка виртуального окружения
log "🐍 Настройка Python окружения..."
sudo -u $USER python3 -m venv env
sudo -u $USER env/bin/pip install --upgrade pip
sudo -u $USER env/bin/pip install -r requirements.txt

# 4. Настройка переменных окружения
log "⚙️ Настройка переменных окружения..."
if [ ! -f .env ]; then
    sudo -u $USER cp env.production .env
    log "✏️ Отредактируйте файл .env со своими данными!"
    echo "DATABASE_URL, DOMAIN и другие настройки"
fi

# 5. Инициализация базы данных
log "🗄️ Инициализация базы данных..."
sudo -u $USER env/bin/alembic upgrade head

# 6. Настройка Nginx
log "🌐 Настройка Nginx..."
sudo cp nginx.conf /etc/nginx/sites-available/ocpp-server
sudo sed -i "s/your-domain.com/$DOMAIN/g" /etc/nginx/sites-available/ocpp-server
sudo ln -sf /etc/nginx/sites-available/ocpp-server /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# 7. Настройка systemd сервиса
log "🔧 Настройка systemd сервиса..."
sudo cp ocpp-server.service /etc/systemd/system/
sudo sed -i "s|/home/ocpp/EvPower-Backend/backend|$APP_DIR|g" /etc/systemd/system/ocpp-server.service
sudo systemctl daemon-reload
sudo systemctl enable ocpp-server

# 8. Запуск сервисов
log "▶️ Запуск сервисов..."
sudo systemctl start ocpp-server
sudo systemctl status ocpp-server --no-pager

# 9. Настройка логротации
log "📊 Настройка логротации..."
sudo tee /etc/logrotate.d/ocpp-server > /dev/null <<EOF
/var/log/ocpp/*.log {
    daily
    missingok
    rotate 52
    compress
    delaycompress
    notifempty
    create 644 $USER $USER
    postrotate
        systemctl reload ocpp-server
    endscript
}
EOF

# 10. Настройка мониторинга
log "📈 Настройка базового мониторинга..."
sudo tee /etc/systemd/system/ocpp-health-check.service > /dev/null <<EOF
[Unit]
Description=OCPP Health Check
After=ocpp-server.service

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -f http://localhost:8000/health
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/ocpp-health-check.timer > /dev/null <<EOF
[Unit]
Description=Run OCPP Health Check every 5 minutes
Requires=ocpp-health-check.service

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ocpp-health-check.timer
sudo systemctl start ocpp-health-check.timer

# 11. Финальная проверка
log "✅ Проверка деплоя..."
sleep 5

if curl -f http://$DOMAIN/health > /dev/null 2>&1; then
    log "🎉 Деплой успешно завершен!"
    log "🌐 API доступен по адресу: http://$DOMAIN"
    log "📚 Документация: http://$DOMAIN/docs"
    log "🔌 WebSocket: ws://$DOMAIN/ws/{station_id}"
else
    log "❌ Ошибка: сервис недоступен"
    log "🔍 Проверьте логи: sudo journalctl -u ocpp-server -f"
    exit 1
fi

echo ""
log "🎯 Следующие шаги:"
echo "1. Настройте DNS: $DOMAIN -> IP сервера"
echo "2. Отредактируйте .env файл с реальными данными"
echo "3. Перезапустите сервис: sudo systemctl restart ocpp-server"
echo "4. Проверьте логи: sudo journalctl -u ocpp-server -f"
echo "5. Подключите зарядные станции к ws://$DOMAIN/ws/{station_id}" 