"""Общие настройки web и worker. Секреты никогда не включаются в профиль попытки."""
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError


def cipher():
    # Отдельный контекст отделяет ключ шифрования от других применений SECRET_KEY.
    digest = hashlib.sha256(("ton:ai-credentials:v1:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def configuration():
    from .models import AIConfiguration
    return AIConfiguration.objects.filter(pk=1).first()


def api_key(provider):
    if provider not in ("openai", "openrouter"):
        return ""
    if getattr(settings, "ALLOW_TEST_EVALUATOR", False):
        return getattr(settings, provider.upper() + "_API_KEY", "")
    config = configuration()
    encrypted = getattr(config, provider + "_secret", "")
    if encrypted:
        try:
            return cipher().decrypt(encrypted.encode()).decode()
        except InvalidToken:
            raise ValidationError("Не удалось расшифровать API-ключ. Введите его заново в настройках нейросети.")
    return getattr(settings, provider.upper() + "_API_KEY", "")
