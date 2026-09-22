"""Проверка локального доступа без вывода пароля в журналы контейнера."""
import json
import os
import sys
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from trainer.models import LoginThrottle
from trainer.services import audit


class Command(BaseCommand):
    help = "Проверить начальный пароль admin или восстановить его из stdin"

    def add_arguments(self, parser):
        parser.add_argument("--reset-admin", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        user = get_user_model().objects.select_for_update().filter(username="admin", is_superuser=True).first()
        if not user:
            raise CommandError("Суперпользователь admin не найден. Используйте manage.py createsuperuser.")
        password = sys.stdin.read().strip() if options["reset_admin"] else os.environ.get("DEMO_PASSWORD", "")
        if options["reset_admin"]:
            if len(password) < 16:
                raise CommandError("Новый пароль должен содержать не менее 16 символов.")
            user.set_password(password)
            user.is_active = True
            user.save(update_fields=["password", "is_active"])
            # Локальное восстановление снимает блокировку после неудачных входов.
            LoginThrottle.objects.all().delete()
            audit(None, "admin_access_recovered", user)
        self.stdout.write(json.dumps({"matches": bool(password) and user.check_password(password) and user.is_active}))
