"""Проверка SMTP без вывода адресов, логина и пароля."""
from django.conf import settings


SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"


class SMTPConfigurationError(ValueError):
    def __init__(self, code, setting, instruction):
        super().__init__(f"{setting}: {instruction}")
        self.code = code
        self.setting = setting


def validate_smtp_configuration():
    if settings.EMAIL_BACKEND != SMTP_BACKEND:
        return
    for name in ("EMAIL_HOST", "DEFAULT_FROM_EMAIL"):
        if not getattr(settings, name, "").strip():
            raise SMTPConfigurationError("SMTP_SETTING_MISSING", name, "значение не задано в .env.")
    # smtplib кодирует ответы AUTH как ASCII. Значения нельзя исправлять
    # удалением или заменой символов: это изменит пароль.
    for name in ("EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD"):
        value = getattr(settings, name, "")
        if value and not value.isascii():
            raise SMTPConfigurationError(
                "SMTP_CREDENTIAL_NON_ASCII", name,
                "есть символы вне ASCII. Укажите SMTP-реквизиты из кабинета почтового провайдера "
                "с латинскими буквами, цифрами и обычными знаками.",
            )
    if bool(settings.EMAIL_HOST_USER) != bool(settings.EMAIL_HOST_PASSWORD):
        missing = "EMAIL_HOST_PASSWORD" if settings.EMAIL_HOST_USER else "EMAIL_HOST_USER"
        raise SMTPConfigurationError("SMTP_SETTING_MISSING", missing,
                                     "для авторизации нужны и SMTP-логин, и SMTP-пароль.")
