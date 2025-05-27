#!/bin/bash

# Скрипт настройки production сервера для OCPP (HTTP версия)
echo "🚀 Настройка OCPP сервера (HTTP)..."

# Обновление системы
echo "📦 Обновление пакетов..."
sudo apt update && sudo apt upgrade -y

# Установка основных зависимостей
echo "🔧 Установка зависимостей..."
sudo apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    git \
    nginx \
    redis-server \
    postgresql \
    postgresql-contrib \
    htop \
    curl \
    wget \
    ufw \
    supervisor

# Настройка PostgreSQL
echo "🗄️ Настройка PostgreSQL..."
sudo systemctl start postgresql
sudo systemctl enable postgresql

# Создание базы данных и пользователя
sudo -u postgres psql << EOF
CREATE DATABASE ocpp_db;
CREATE USER ocpp_user WITH ENCRYPTED PASSWORD 'secure_password_here';
GRANT ALL PRIVILEGES ON DATABASE ocpp_db TO ocpp_user;
ALTER USER ocpp_user CREATEDB;
\q
EOF

# Настройка Redis
echo "📡 Настройка Redis..."
sudo systemctl start redis-server
sudo systemctl enable redis-server

# Создание пользователя для приложения
echo "👤 Создание пользователя..."
sudo useradd -m -s /bin/bash ocpp
sudo usermod -aG sudo ocpp

# Настройка firewall
echo "🔒 Настройка firewall..."
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 80
sudo ufw allow 8000  # OCPP WebSocket
sudo ufw allow 8180  # Дополнительный OCPP порт
sudo ufw --force enable

echo "✅ Базовая настройка сервера завершена!"
echo "📋 Следующие шаги:"
echo "1. Клонируйте проект в /home/ocpp/"
echo "2. Настройте виртуальное окружение"
echo "3. Запустите сервисы" 