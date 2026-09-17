"""Отдельный одноразовый контейнер исключает параллельный запуск миграций."""
import os
import subprocess
import sys

for command in (["migrate", "--noinput"], ["collectstatic", "--noinput"], ["seed_demo"]):
    subprocess.run([sys.executable, "manage.py", *command], check=True)
