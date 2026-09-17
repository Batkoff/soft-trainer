"""Сохраняет базу Docker-демо в backups/. Работает и в Windows.

Передаём бинарный pg_dump напрямую в файл: shell-перенаправление старого
PowerShell может изменить кодировку и повредить архив.
"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parent.parent
folder = root / "backups"
folder.mkdir(exist_ok=True)
path = folder / f"trainer-{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}.dump"
created = False
try:
    with path.open("xb") as output:
        created = True
        subprocess.run(["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "trainer",
                        "-d", "trainer", "-Fc"], cwd=root, stdout=output, check=True)
except Exception:
    if created:
        path.unlink(missing_ok=True)
    raise
print(f"Резервная копия: {path}")
