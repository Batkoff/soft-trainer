"""Запуск с проверкой Docker и ожиданием готовности сайта. Python 3.10+."""
import argparse
import shutil
import subprocess

from prepare_demo import ROOT, hostname, prepare


def main():
    parser = argparse.ArgumentParser(description="Запустить Тон через Docker Compose")
    parser.add_argument("--host", type=hostname, help="IP/домен для доступа по сети")
    parser.add_argument("--port", type=int, help="Внешний порт сайта")
    args = parser.parse_args()
    if not shutil.which("docker"):
        parser.exit(1, "Docker не найден. Установите и откройте Docker Desktop. См. docs/GETTING_STARTED.md\n")
    try:
        subprocess.run(["docker", "compose", "version"], check=True, timeout=20)
        subprocess.run(["docker", "info"], check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        parser.exit(1, "Docker не готов. Запустите Docker Desktop в режиме Linux containers и дождитесь запуска движка.\n")
    try:
        settings = prepare(host=args.host, port=args.port)
        # Полный compose config раскрывает секреты, используем только проверку.
        subprocess.run(["docker", "compose", "config", "--quiet"], cwd=ROOT, check=True)
        subprocess.run(["docker", "compose", "up", "--build", "--wait", "--wait-timeout", "180"], cwd=ROOT, check=True)
    except (ValueError, OSError):
        parser.exit(1, "Проверьте .env и доступ на запись в папку проекта. HTTP_PORT: число от 1 до 65535.\n")
    except subprocess.CalledProcessError:
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
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("\nОжидание прервано. Состояние контейнеров: docker compose ps -a")
