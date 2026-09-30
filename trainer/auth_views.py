"""Публичные страницы регистрации и подтверждения адреса."""
import hashlib
import hmac
import logging
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_http_methods

from .forms import RegistrationForm
from .models import AuditEvent

logger = logging.getLogger("trainer.registration")


def _registration_ref(email):
    normalized = (email or "").strip().casefold()[:254]
    if not normalized:
        return "unknown"
    return hmac.new(settings.SECRET_KEY.encode(), normalized.encode(), hashlib.sha256).hexdigest()[:24]


def _registration_event(request, action, reference, **details):
    AuditEvent.objects.create(actor=None, action=action, object_id=f"registration:{reference}",
        details={"request_id": getattr(request, "request_id", ""), **details})


class ActivationEmailError(Exception):
    def __init__(self, cause_type, stage="unknown", safe_metadata=None):
        super().__init__(cause_type)
        self.cause_type = cause_type
        self.stage = stage
        self.safe_metadata = safe_metadata or {}


def _safe_email_failure_metadata(error):
    if isinstance(error, UnicodeEncodeError):
        return {"encoding": error.encoding, "reason": error.reason,
                "start": error.start, "end": error.end}
    return {}


def _non_ascii_email_settings(email):
    values = {
        "smtp_username": settings.EMAIL_HOST_USER,
        "smtp_password": settings.EMAIL_HOST_PASSWORD,
        "sender_address": settings.DEFAULT_FROM_EMAIL,
        "recipient_address": email,
    }
    return sorted(name for name, value in values.items() if value and not value.isascii())


def _activation_url(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("activate", kwargs={"uidb64": uid, "token": token}))


def _send_activation_email(request, user):
    if settings.EMAIL_BACKEND.endswith("smtp.EmailBackend") and not settings.EMAIL_HOST.strip():
        raise ActivationEmailError("SMTP_HOST_NOT_CONFIGURED", "smtp_configuration")
    try:
        activation_url = _activation_url(request, user)
        body = render_to_string("registration/activation_email.txt", {
            "user": user, "activation_url": activation_url,
        })
    except Exception as error:
        raise ActivationEmailError(type(error).__name__, "message_rendering",
                                   _safe_email_failure_metadata(error)) from error
    try:
        sent = send_mail(
            "Подтвердите почту — Тон",
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )
    except Exception as error:
        raise ActivationEmailError(type(error).__name__, "smtp_delivery",
                                   _safe_email_failure_metadata(error)) from error
    if sent != 1:
        raise ActivationEmailError("SMTP_NO_MESSAGE_ACCEPTED", "smtp_delivery")
    return sent


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST":
        if not form.is_valid():
            reference = _registration_ref(form.data.get("email", ""))
            _registration_event(request, "registration_rejected", reference,
                                invalid_fields=sorted(form.errors.keys()))
            return render(request, "registration/register.html", {"form": form})
        reference = _registration_ref(form.cleaned_data["email"])
        _registration_event(request, "registration_started", reference)
        try:
            with transaction.atomic():
                user = form.save()
                try:
                    _send_activation_email(request, user)
                except ActivationEmailError:
                    raise
                except Exception as error:
                    raise ActivationEmailError(type(error).__name__) from error
        except ActivationEmailError as error:
            smtp_configuration = {
                "backend": settings.EMAIL_BACKEND.rsplit(".", 1)[-1],
                "host_configured": bool(settings.EMAIL_HOST.strip()),
                "sender_configured": bool(settings.DEFAULT_FROM_EMAIL.strip()),
                "username_configured": bool(settings.EMAIL_HOST_USER.strip()),
            }
            _registration_event(request, "registration_email_failed", reference,
                                exception_type=error.cause_type, failure_stage=error.stage,
                                exception_metadata=error.safe_metadata,
                                non_ascii_components=_non_ascii_email_settings(form.cleaned_data["email"]),
                                smtp_configuration=smtp_configuration)
            logger.warning("registration_email_failed", extra={"context": {
                "request_id": getattr(request, "request_id", ""), "email_fingerprint": reference,
                "exception_type": error.cause_type, "failure_stage": error.stage,
                "non_ascii_components": _non_ascii_email_settings(form.cleaned_data["email"]),
            }})
            request_id = getattr(request, "request_id", "не указан")
            form.add_error(None, f"Не удалось отправить письмо. Аккаунт не создан. Попробуйте позже; если ошибка повторится, сообщите администратору код {request_id}.")
        else:
            _registration_event(request, "registration_created", reference, user_id=user.pk)
            return render(request, "registration/check_email.html", {"email": user.email})
    return render(request, "registration/register.html", {"form": form})


@require_http_methods(["GET"])
def activate(request, uidb64, token):
    User = get_user_model()
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist, Http404):
        user = None
    if user is not None and not user.is_active and default_token_generator.check_token(user, token):
        user.is_active = True
        user.save(update_fields=["is_active"])
        _registration_event(request, "registration_activated", _registration_ref(user.email), user_id=user.pk)
        messages.success(request, "Почта подтверждена. Теперь войдите — роль и доступ к тренировке назначает администратор.")
        return redirect("login")
    if user is not None:
        _registration_event(request, "registration_activation_rejected", _registration_ref(user.email))
    return render(request, "registration/activation_invalid.html", status=400)
