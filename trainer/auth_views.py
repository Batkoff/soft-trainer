"""Публичные страницы регистрации и подтверждения адреса."""
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


def _activation_url(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("activate", kwargs={"uidb64": uid, "token": token}))


def _send_activation_email(request, user):
    activation_url = _activation_url(request, user)
    body = render_to_string("registration/activation_email.txt", {
        "user": user, "activation_url": activation_url,
    })
    sent = send_mail(
        "Подтвердите почту — Тон",
        body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )
    if sent != 1:
        raise RuntimeError("SMTP не принял письмо")
    return sent


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = None
        try:
            with transaction.atomic():
                user = form.save()
                _send_activation_email(request, user)
        except Exception:
            # Не оставляем неактивную учётную запись, если SMTP недоступен:
            # человек сможет повторить регистрацию после исправления .env.
            if user is not None:
                user.delete()
            form.add_error(None, "Не удалось отправить письмо. Проверьте почту или обратитесь к администратору.")
        else:
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
        messages.success(request, "Почта подтверждена. Теперь войдите — роль и доступ к тренировке назначает администратор.")
        return redirect("login")
    return render(request, "registration/activation_invalid.html", status=400)
