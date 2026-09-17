"""Структурированные логи без паролей, cookie и текстов клиентских ответов."""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                "logger": record.name, "message": record.getMessage()}
        data.update(getattr(record, "context", {}))
        if record.exc_info:
            # Тексты исключений провайдеров могут содержать запрос. В лог пишем только тип.
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False, default=str)

class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = str(uuid.uuid4())
        start = time.monotonic()
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        if not request.path.startswith("/static/"):
            logging.getLogger("trainer.http").info("http_request", extra={"context": {
                "request_id": request.request_id, "method": request.method, "path": request.path,
                "status": response.status_code, "duration_ms": round((time.monotonic()-start)*1000),
                "user_id": request.user.pk if request.user.is_authenticated else None,
            }})
        return response
