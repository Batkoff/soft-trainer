"""Настройка локального запуска без зависимостей. Существующие секреты сохраняются."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets

ROOT = Path(__file__).resolve().parent.parent


def read_env(path):
    """Читаем простые значения dotenv, не выполняя файл как код."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def private_write(path, text):
    """Новый файл сразу создаётся приватным; секреты не попадают в stdout."""
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def hostname(value):
    host = value.strip().strip("[]")
    try:
        address = ipaddress.ip_address(host)
        return f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        if len(host) <= 253 and all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part) for part in host.split(".")):
            return host.lower()
    raise argparse.ArgumentTypeError("Укажите IP или домен без http://, порта и пути.")


def prepare(root=ROOT, host=None, port=None):
    path = root / ".env"
    values = read_env(path)
    defaults = {
        "SECRET_KEY": secrets.token_urlsafe(48), "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "DEMO_PASSWORD": secrets.token_urlsafe(16), "DEBUG": "0",
        "ALLOWED_HOSTS": "localhost,127.0.0.1,[::1]",
        "CSRF_TRUSTED_ORIGINS": "http://localhost:8080,http://127.0.0.1:8080",
        "HTTP_BIND": "127.0.0.1", "HTTP_PORT": "8080", "SECURE_COOKIES": "0",
        "DEMO_EVALUATION_DELAY": "2", "LOG_LEVEL": "INFO", "EVALUATOR_BACKEND": "demo",
        "EVALUATOR_MODEL": "", "OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "",
    }
    updates = {key: value for key, value in defaults.items() if key not in values}
    # Поддерживает ручное копирование .env.example с пустыми секретами.
    for key in ("SECRET_KEY", "POSTGRES_PASSWORD", "DEMO_PASSWORD"):
        if not values.get(key):
            updates[key] = defaults[key]
    settings = {**values, **updates}
    selected_port = port if port is not None else int(settings["HTTP_PORT"])
    if not 1 <= selected_port <= 65535:
        raise ValueError("HTTP_PORT должен быть числом от 1 до 65535.")
    if port is not None:
        updates["HTTP_PORT"] = str(port)
    if host or port is not None:
        hosts = [item for item in settings["ALLOWED_HOSTS"].split(",") if item]
        origins = [item for item in settings["CSRF_TRUSTED_ORIGINS"].split(",") if item]
        if host:
            hosts.append(host)
            updates["HTTP_BIND"] = "0.0.0.0"
        # При смене порта сохраняем работоспособность ранее добавленного IP.
        for name in hosts:
            if name and name != "*" and not name.startswith("."):
                origins.append(f"http://{name}:{selected_port}")
        updates["ALLOWED_HOSTS"] = ",".join(dict.fromkeys(hosts))
        updates["CSRF_TRUSTED_ORIGINS"] = ",".join(dict.fromkeys(origins))
    if updates:
        lines, seen = [], set()
        original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        for line in original.splitlines():
            key = line.split("=", 1)[0].strip()
            if key in updates:
                if key not in seen:
                    lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                lines.append(line)
        lines.extend(f"{key}={value}" for key, value in updates.items() if key not in seen)
        private_write(path, "\n".join(lines) + "\n")
    settings.update(updates)
    credentials = root / ".demo-credentials.txt"
    if not credentials.exists():
        private_write(credentials, "Тон · данные первого запуска\nАдминистратор: admin\n"
                      "Сотрудники: demo1, demo2, demo3, demo4, demo5\n"
                      f"Начальный пароль: {settings['DEMO_PASSWORD']}\n"
                      "Если пароль меняли в админке, используйте новый. Обновление его не сбрасывает.\n")
    return settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Подготовить Тон к запуску")
    parser.add_argument("--host", type=hostname, help="IP/домен для доступа с другого компьютера")
    parser.add_argument("--port", type=int, help="Порт сайта (по умолчанию 8080)")
    args = parser.parse_args(argv)
    try:
        settings = prepare(host=args.host, port=args.port)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Не удалось подготовить настройки: {error}\n")
    print("Настройки готовы. Существующие ключи и пароли сохранены.")
    print("Данные входа: .demo-credentials.txt (не отправляйте этот файл другим).")
    print(f"Следующий шаг: python scripts/start.py — порт {settings['HTTP_PORT']}.")


if __name__ == "__main__":
    main()
