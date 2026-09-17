"""Создаёт .env из безопасного шаблона один раз.

Используется и на Windows, и на VPS. Существующий .env никогда не перезаписывается.
"""
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".env"

if TARGET.exists():
    print(".env уже существует — оставил настройки без изменений.")
    sys.exit(0)

TARGET.write_text("\n".join([
    f"SECRET_KEY={secrets.token_urlsafe(48)}",
    f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
    f"DEMO_PASSWORD={secrets.token_urlsafe(16)}",
    "DEBUG=0", "ALLOWED_HOSTS=localhost,127.0.0.1,[::1]",
    "CSRF_TRUSTED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080",
    "HTTP_BIND=0.0.0.0", "HTTP_PORT=8080", "SECURE_COOKIES=0",
    "DEMO_EVALUATION_DELAY=2", "LOG_LEVEL=INFO", "EVALUATOR_BACKEND=demo",
    "EVALUATOR_MODEL=", "OPENAI_API_KEY=", "OPENROUTER_API_KEY=", "",
]), encoding="utf-8")
try:
    TARGET.chmod(0o600)
except OSError:
    pass
print("Создан .env с новыми секретами. Используется demo-оценка без API-ключей.")
print("Для OpenRouter: python scripts/configure_ai.py")
