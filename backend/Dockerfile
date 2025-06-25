FROM python:3.11-slim

# Установка curl для health checks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh

EXPOSE 8180

CMD ["/app/start.sh"]