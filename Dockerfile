FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh

EXPOSE 9210

CMD ["/app/start.sh"]