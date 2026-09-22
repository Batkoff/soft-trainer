"""Запуск с проверкой Docker и ожиданием готовности сайта. Python 3.10+."""
import argparse
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

if __package__:
    from .prepare_demo import ROOT, hostname, prepare, read_env
else:
    from prepare_demo import ROOT, hostname, prepare, read_env


# exec -T намеренно не открывает TTY: Git Bash не зависает на приглашениях psql.
# Пароль берётся внутри контейнера, а не передаётся в аргументах процесса.
APP_PROBE = ("run", "--rm", "--no-deps", "-T", "init", "python", "scripts/check_database.py")
SYNC_PASSWORD = r'''printf '%s\n%s\n' "$POSTGRES_PASSWORD" "$POSTGRES_PASSWORD" | psql -X -w -U trainer -d trainer -v ON_ERROR_STOP=1 -c '\password trainer' '''


def compose(*args, **kwargs):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, **kwargs)


def redact(output):
    for key, value in read_env(ROOT / ".env").items():
        if value and any(part in key for part in ("PASSWORD", "SECRET", "API_KEY", "TOKEN")):
            output = output.replace(value, "[скрыто]")
    return re.sub(r"(postgres(?:ql)?://[^:\s/@]+:)[^@\s]+@", r"\1[скрыто]@", output)


def ensure_database():
    """Поднимает собственную БД проекта и согласует пароль без удаления тома."""
    print("[3/4] Запускаю PostgreSQL и проверяю подключение из Django…", flush=True)
    compose("up", "-d", "--wait", "--wait-timeout", "120", "db", check=True)
    probe = compose(*APP_PROBE, capture_output=True, text=True, timeout=90)
    if probe.returncode == 0:
        return
    if probe.returncode != 42:
        raise RuntimeError("PostgreSQL не прошёл проверку подключения. "
                           "Данные сохранены. Вывод проверки:\n" + redact(probe.stdout + probe.stderr)[-5000:])
    print("Пароль сохранённой базы отличается от настроек. Согласую его автоматически…", flush=True)
    # Локальное администрирование разрешено штатной конфигурацией нашего образа.
    # Если владелец ограничил его, не меняем pg_hba.conf и не ослабляем авторизацию.
    access = compose("exec", "-T", "db", "psql", "-X", "-w", "-U", "trainer",
                     "-d", "trainer", "-Atqc", "SELECT 1",
                     capture_output=True, timeout=30)
    if access.returncode:
        raise RuntimeError("В этой базе запрещено локальное администрирование. "
                           "Автовосстановление остановлено; база не изменена. Нужен её исходный пароль.")
    # \password шифрует пароль до отправки ALTER ROLE. Не пишем пароль в SQL-лог.
    repair = compose("exec", "-T", "db", "sh", "-c", SYNC_PASSWORD,
                     capture_output=True, timeout=30)
    if repair.returncode:
        raise RuntimeError("Не удалось согласовать пароль PostgreSQL. Данные не удалены.")
    verify = compose(*APP_PROBE, capture_output=True, timeout=90)
    if verify.returncode:
        raise RuntimeError("Пароль изменён, но проверка подключения не прошла. "
                           "Проверьте: docker compose logs --tail=80 db")
    print("Подключение восстановлено. Пользователи, задания и ответы сохранены.", flush=True)


def show_failure_logs():
    """Ошибка init сразу видна в том же окне; известные секреты скрываются."""
    try:
        logs = compose("logs", "--no-color", "--tail=60", "init", "web", "worker", "proxy", "db",
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        output = redact(logs.stdout + logs.stderr)
        print("\nЖурнал запуска (секреты скрыты):\n" + output, flush=True)
    except (OSError, subprocess.SubprocessError):
        print("Не удалось получить журнал автоматически: docker compose logs --tail=60 init", flush=True)


def ensure_docker():
    subprocess.run(["docker", "compose", "version"], check=True, timeout=20)

    def ready():
        try:
            return subprocess.run(["docker", "info"], timeout=10,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    if ready():
        return
    if os.name == "nt":
        desktop = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker/Docker/Docker Desktop.exe"
        if desktop.exists():
            print("Открываю Docker Desktop. Ожидаю запуск движка (до двух минут)…", flush=True)
            subprocess.Popen([str(desktop)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if ready():
                    return
                time.sleep(2)
    raise RuntimeError("Docker Engine недоступен. Проверьте Docker Desktop и режим Linux containers. "
                       "Конкретная ошибка: docker info")


def main():
    parser = argparse.ArgumentParser(description="Запустить Тон через Docker Compose")
    parser.add_argument("--host", type=hostname, help="IP/домен для доступа по сети")
    parser.add_argument("--port", type=int, help="Внешний порт сайта")
    args = parser.parse_args()
    if not shutil.which("docker"):
        parser.exit(1, "Docker не найден. Установите и откройте Docker Desktop. См. docs/GETTING_STARTED.md\n")
    try:
        ensure_docker()
        print("[1/4] Проверяю настройки и сохраняю существующие секреты…", flush=True)
        settings = prepare(host=args.host, port=args.port)
        # Полный compose config раскрывает секреты, используем только проверку.
        compose("config", "--quiet", check=True)
        print("[2/4] Собираю приложение…", flush=True)
        compose("build", check=True)
        ensure_database()
        print("[4/4] Применяю миграции и запускаю сайт с очередью…", flush=True)
        compose("up", "--wait", "--wait-timeout", "180", check=True)
    except RuntimeError as error:
        show_failure_logs()
        parser.exit(1, f"{error}\n")
    except subprocess.TimeoutExpired:
        parser.exit(1, "Docker не ответил вовремя. Данные сохранены. Повторите запуск после проверки docker info.\n")
    except (ValueError, OSError):
        parser.exit(1, "Проверьте .env и доступ на запись в папку проекта. HTTP_PORT: число от 1 до 65535.\n")
    except subprocess.CalledProcessError:
        show_failure_logs()
        parser.exit(1, "Запуск не завершён. Причина выше. Проверка: docker compose ps -a\n"
                       "Логи: docker compose logs --tail=100 init web worker proxy\n"
                       "Решения: docs/GETTING_STARTED.md#если-не-запустилось\n")
    site_address = settings.get("SITE_ADDRESS", ":80")
    if site_address != ":80":
        print(f"\nКонтейнеры готовы. Адрес сайта: https://{site_address}")
        print("Выдачу HTTPS-сертификата проверьте в docker compose logs proxy.")
    else:
        print(f"\nТон готов: http://localhost:{settings['HTTP_PORT']}")
    if args.host and site_address == ":80":
        print(f"Адрес в сети: http://{args.host}:{settings['HTTP_PORT']}")
    print("Логин и начальный пароль: .demo-credentials.txt")


if __name__ == "__main__":
    # Windows Python под Git Bash может выбрать cp1251 вместо UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("\nОжидание прервано. Состояние контейнеров: docker compose ps -a")
