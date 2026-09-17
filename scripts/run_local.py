"""Локальное демо для Linux/macOS: встроенный PostgreSQL, Django и два процесса.

Запускать обычным пользователем. Для Windows используйте Docker Compose.
Пакет pgserver нужен только разработчику; в Docker используется PostgreSQL 17.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
os.chdir(root)
if hasattr(os, "geteuid") and os.geteuid() == 0:
    raise SystemExit("Локальный PostgreSQL запускается обычным пользователем. Под root используйте Docker Compose.")
try:
    import pgserver
except ImportError:
    raise SystemExit("Установите зависимости: pip install -r requirements-dev.txt")
data = root / ".data"
data.mkdir(exist_ok=True)
server = pgserver.get_server(data / "postgres", cleanup_mode="stop")
subprocess.run([sys.executable, "scripts/prepare_demo.py"], check=True)
# Используем те же секреты, что и Docker. Файл не выполняется как shell-код.
settings = {}
for line in (root / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip() and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip()
env = {**os.environ, **settings, "DATABASE_URL": server.get_uri(), "DEBUG": "1"}
for command in (["migrate", "--noinput"], ["seed_demo"]):
    subprocess.run([sys.executable, "manage.py", *command], env=env, check=True)
processes = [subprocess.Popen([sys.executable, "manage.py", "procrastinate", "worker", "--concurrency", "2"], env=env),
             subprocess.Popen([sys.executable, "manage.py", "runserver", "127.0.0.1:8000", "--noreload"], env=env)]
print("Откройте http://127.0.0.1:8000 · данные для входа: .demo-credentials.txt", flush=True)
try:
    while all(process.poll() is None for process in processes):
        time.sleep(0.3)
    raise SystemExit("Один из процессов завершился. Причина указана в журнале выше.")
except KeyboardInterrupt:
    pass
finally:
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
