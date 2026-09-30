"""Проверяет настройки, соединение и вход на SMTP без отправки письма."""
from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError

from trainer.email_configuration import SMTP_BACKEND, SMTPConfigurationError, validate_smtp_configuration


class Command(BaseCommand):
    help = "Проверить SMTP-настройки и авторизацию без отправки писем и вывода секретов."
    requires_system_checks = []

    def handle(self, *args, **options):
        if settings.EMAIL_BACKEND != SMTP_BACKEND:
            raise CommandError("EMAIL_BACKEND не использует SMTP. Проверьте .env.")
        try:
            validate_smtp_configuration()
        except SMTPConfigurationError as error:
            raise CommandError(str(error)) from None
        try:
            with get_connection(fail_silently=False):
                pass
        except Exception as error:
            # Ответ провайдера может содержать адрес или логин — выводим только тип и код.
            code = getattr(error, "smtp_code", None)
            suffix = f" (код {code})" if isinstance(code, int) else ""
            raise CommandError(
                f"Проверка SMTP не пройдена: {type(error).__name__}{suffix}. "
                "Проверьте настройки .env и SMTP-реквизиты у провайдера."
            ) from None
        message = "SMTP-соединение установлено."
        if settings.EMAIL_HOST_USER:
            message += " Авторизация успешна."
        self.stdout.write(self.style.SUCCESS(message))
        self.stdout.write("Письма не отправлялись. Доставка и разрешённый адрес отправителя проверяются отдельно.")
