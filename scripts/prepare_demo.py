"""Однократно создаёт локальные секреты. Повторный запуск ничего не перезаписывает."""
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / ".env"
if path.exists():
    print(".env уже существует. Настройки сохранены без изменений.")
else:
    password = secrets.token_urlsafe(12)
    lines = [f"SECRET_KEY={secrets.token_urlsafe(48)}", f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
        f"DEMO_PASSWORD={password}", "DEBUG=0", "ALLOWED_HOSTS=localhost,127.0.0.1,[::1]",
        "CSRF_TRUSTED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080", "SECURE_COOKIES=0",
        "HTTP_BIND=0.0.0.0", "HTTP_PORT=8080",
        "DEMO_EVALUATION_DELAY=2", "LOG_LEVEL=INFO", "EVALUATOR_BACKEND=demo", "EVALUATOR_MODEL=",
        "OPENAI_API_KEY=", "OPENROUTER_API_KEY="]
    path.write_text("\n".join(lines)+"\n", encoding="utf-8")
    credentials = root / ".demo-credentials.txt"
    credentials.write_text(f"Администратор: admin\nСотрудники: demo1, demo2, demo3, demo4, demo5\nПароль демо-аккаунтов: {password}\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
        credentials.chmod(0o600)
    print("Настройки созданы. Логины и пароль находятся в .demo-credentials.txt.")
