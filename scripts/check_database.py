"""Проверка из контейнера приложения теми же настройками, что использует Django."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main():
    import django
    django.setup()
    from django.db import connection, OperationalError
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except OperationalError as error:
        # Ошибка подключения libpq не всегда содержит sqlstate.
        cause = error.__cause__
        if getattr(cause, "sqlstate", None) == "28P01" or "password authentication failed" in str(error):
            print("DATABASE_AUTH_FAILED", flush=True)
            return 42
        print("DATABASE_CONNECTION_FAILED", flush=True)
        return 1
    finally:
        connection.close()
    print("DATABASE_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
