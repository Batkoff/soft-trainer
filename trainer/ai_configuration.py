"""Общие настройки web и worker. Секреты никогда не включаются в профиль попытки."""
import base64
import hashlib
from urllib.parse import quote, urlsplit, urlunsplit
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


def _decrypt(value, label):
    if not value:
        return ""
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        raise ValidationError(f"Не удалось расшифровать {label}. Введите его заново в настройках нейросети.")


def api_key(provider):
    if provider not in ("openai", "openrouter"):
        return ""
    if getattr(settings, "ALLOW_TEST_EVALUATOR", False):
        return getattr(settings, provider.upper() + "_API_KEY", "")
    config = configuration()
    encrypted = getattr(config, provider + "_secret", "") if config else ""
    if encrypted:
        return _decrypt(encrypted, "API-ключ")
    return getattr(settings, provider.upper() + "_API_KEY", "")


def proxy_settings():
    """Текущая инфраструктурная настройка применяется к новым HTTP-запросам сразу."""
    config = configuration()
    if not config or not config.proxy_enabled:
        return {"enabled": False, "url": "", "username": "", "password": ""}
    return {
        "enabled": True,
        "url": (config.proxy_url or "").strip(),
        "username": (config.proxy_username or "").strip(),
        "password": _decrypt(config.proxy_secret, "пароль прокси"),
    }


def outbound_proxy_url():
    """URL для urllib.ProxyHandler с безопасно закодированными Basic Auth данными."""
    proxy = proxy_settings()
    if not proxy["enabled"]:
        return ""
    parsed = urlsplit(proxy["url"])
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("У прокси указан некорректный порт.") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not port:
        raise ValidationError("Укажите прокси как http://host:port или https://host:port.")
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValidationError("В адресе прокси оставьте только схему, хост и порт. Логин и пароль вводятся отдельно.")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if proxy["username"]:
        auth = quote(proxy["username"], safe="")
        if proxy["password"]:
            auth += ":" + quote(proxy["password"], safe="")
        auth += "@"
    return urlunsplit((parsed.scheme, f"{auth}{host}:{port}", "", "", ""))
