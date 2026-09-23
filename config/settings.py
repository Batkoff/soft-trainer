"""Все настройки среды — здесь. Секреты приходят только из окружения."""
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv("DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Задайте SECRET_KEY. Для локального демо используйте scripts/start.py.")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
CSRF_TRUSTED_ORIGINS = [x for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if x]
DATABASE_URL = os.environ.get("DATABASE_URL", "")
db = urlparse(DATABASE_URL)
if db.scheme not in ("postgres", "postgresql"):
    raise ImproperlyConfigured("DATABASE_URL должен указывать на PostgreSQL.")
options = {key: values[-1] for key, values in parse_qs(db.query).items()}
host = options.pop("host", db.hostname or "localhost")
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql", "NAME": unquote(db.path.lstrip("/")) or "postgres",
    # Compose передаёт пароль отдельно: символы %, #, @ и / не являются частью URL.
    "USER": unquote(db.username or "postgres"), "PASSWORD": os.environ.get("DATABASE_PASSWORD", unquote(db.password or "")),
    "HOST": host, "PORT": db.port or options.pop("port", "5432"),
    "CONN_MAX_AGE": 60, "CONN_HEALTH_CHECKS": True, "OPTIONS": options,
}}
INSTALLED_APPS = [
    "config.admin.TonAdminConfig", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "procrastinate.contrib.django", "trainer",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware", "trainer.observability.RequestLogMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages",
        "trainer.context_processors.evaluator_context"]}}]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_COOKIE_SECURE = os.getenv("SECURE_COOKIES", "0") == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
# Gunicorn не публикует порт наружу; единственная точка входа — Caddy,
# который выставляет X-Forwarded-Proto по фактическому соединению клиента.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# HTML-форма кодирует кириллицу и эмодзи в процентах: 20 000 символов
# промпта могут занимать до 240 КБ в запросе. Лимиты полей остаются в формах.
DATA_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024
EVALUATOR_BACKEND = os.getenv("EVALUATOR_BACKEND", "demo").strip().lower()
if EVALUATOR_BACKEND not in ("demo", "openai", "openrouter"):
    raise ImproperlyConfigured("EVALUATOR_BACKEND: demo, openai или openrouter.")
EVALUATOR_MODEL = os.getenv("EVALUATOR_MODEL", "").strip() or {
    "demo": "demo-v1", "openai": "gpt-4.1-mini-2025-04-14", "openrouter": "openai/gpt-4.1-mini",
}[EVALUATOR_BACKEND]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
EVALUATOR_TIMEOUT = 60  # Тайм-аут сетевой операции; очередь не занимает время сотрудника.
EVALUATOR_MAX_TOKENS = 2400
DEMO_EVALUATION_DELAY = float(os.getenv("DEMO_EVALUATION_DELAY", "2"))
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"json": {"()": "trainer.observability.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
