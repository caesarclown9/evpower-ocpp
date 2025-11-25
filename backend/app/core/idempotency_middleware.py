import hashlib
import json
import uuid
from typing import Optional

from fastapi import Request
from starlette.responses import Response, JSONResponse
from sqlalchemy import text

from app.db.session import get_session_local


def _canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


class IdempotencyMiddleware:
    """Middleware идемпотентности для POST мутаций.

    Работает только на маршрутах:
      - /api/v1/charging/start
      - /api/v1/charging/stop
      - /api/v1/balance/topup-qr
      - /api/v1/balance/topup-card
    """

    TARGET_PATHS = {
        ("POST", "/api/v1/charging/start"),
        ("POST", "/api/v1/charging/stop"),
        ("POST", "/api/v1/balance/topup-qr"),
        ("POST", "/api/v1/balance/topup-card"),
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if (method, path) not in self.TARGET_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        idem_key = request.headers.get("idempotency-key")
        if not idem_key:
            # Генерируем UUID автоматически если клиент не передал
            # Это обеспечивает совместимость с мобильным приложением
            idem_key = f"auto-{uuid.uuid4()}"

        # Читаем тело запроса и восстанавливаем для downstream
        body_bytes = await request.body()
        body_obj: dict = {}
        if body_bytes:
            try:
                body_obj = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                body_obj = {}
        body_hash = hashlib.sha256(_canonical_json(body_obj).encode()).hexdigest()

        # Проверяем запись в БД
        SessionLocal = get_session_local()
        db = SessionLocal()
        try:
            row = db.execute(
                text(
                    """
                    SELECT key, method, path, body_hash, response_json, status_code
                    FROM idempotency_keys
                    WHERE key = :key
                    """
                ),
                {"key": idem_key},
            ).fetchone()

            if row:
                if row.body_hash != body_hash or row.method != method or row.path != path:
                    response = JSONResponse(
                        status_code=409,
                        content={"success": False, "error": "invalid_request", "message": "Idempotency-Key conflict"},
                    )
                    await response(send)
                    return

                # Возвращаем сохраненный ответ
                saved_json = row.response_json
                status_code = int(row.status_code)
                response = JSONResponse(status_code=status_code, content=saved_json)
                await response(send)
                return

            # Нет записи — перехватываем ответ для сохранения
            captured_body: Optional[bytes] = None
            captured_status: Optional[int] = None
            headers_list = []

            async def send_wrapper(message):
                nonlocal captured_body, captured_status, headers_list
                if message["type"] == "http.response.start":
                    captured_status = message["status"]
                    headers_list = message.get("headers", [])
                    # Эхо заголовка Idempotency-Key в ответ
                    try:
                        headers_list.append((b"idempotency-key", idem_key.encode("utf-8")))
                        message["headers"] = headers_list
                    except Exception:
                        # Ничего страшного, если не удалось
                        pass
                elif message["type"] == "http.response.body":
                    body_part = message.get("body", b"")
                    captured_body = (captured_body or b"") + body_part
                await send(message)

            # Восстановим тело для downstream обработчиков
            async def receive_wrapper():
                return {"type": "http.request", "body": body_bytes, "more_body": False}

            await self.app(scope, receive_wrapper, send_wrapper)

            # Пытаемся распарсить JSON ответа
            resp_json = {}
            try:
                if captured_body:
                    resp_json = json.loads(captured_body.decode("utf-8"))
            except Exception:
                resp_json = {}

            try:
                db.execute(
                    text(
                        """
                        INSERT INTO idempotency_keys (key, method, path, body_hash, response_json, status_code)
                        VALUES (:key, :method, :path, :body_hash, :response_json, :status_code)
                        """
                    ),
                    {
                        "key": idem_key,
                        "method": method,
                        "path": path,
                        "body_hash": body_hash,
                        "response_json": json.dumps(resp_json, ensure_ascii=False),
                        "status_code": captured_status or 200,
                    },
                )
                db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()


