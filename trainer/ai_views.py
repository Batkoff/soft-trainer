"""Настройки подключения доступны только владельцу проекта."""
from urllib.parse import urlsplit
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from .ai_configuration import api_key, cipher, configuration
from .evaluation_http import CHECK_ENDPOINTS, check_provider_connection
from .evaluation_profiles import current_profile, profile_has_key
from .forms import AIConfigurationForm
from .models import AIConfiguration
from .services import audit


@never_cache
@sensitive_post_parameters("api_key", "proxy_password")
@login_required
@require_http_methods(["GET", "POST"])
def ai_settings(request):
    if not request.user.is_superuser:
        raise PermissionDenied

    profile = current_profile()
    config = configuration()
    choices = dict(AIConfigurationForm.base_fields["model_choice"].choices)
    initial = {
        "provider": profile["provider"],
        "model_choice": profile["model"] if profile["model"] in choices else "custom",
        "custom_model": profile["model"] if profile["model"] not in choices else "",
        "proxy_enabled": bool(config and config.proxy_enabled),
        "proxy_url": config.proxy_url if config else "",
        "proxy_username": config.proxy_username if config else "",
    }
    form = AIConfigurationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        action = request.POST.get("action", "save")
        with transaction.atomic():
            AIConfiguration.objects.get_or_create(pk=1, defaults={"model": profile["model"]})
            config = AIConfiguration.objects.select_for_update().get(pk=1)
            config.provider = form.cleaned_data["provider"]
            config.model = form.cleaned_data["model"]
            key = form.cleaned_data["api_key"]
            if key:
                setattr(config, config.provider + "_secret", cipher().encrypt(key.encode()).decode())

            config.proxy_enabled = form.cleaned_data["proxy_enabled"]
            config.proxy_url = form.cleaned_data["proxy_url"].strip()
            config.proxy_username = form.cleaned_data["proxy_username"].strip()
            proxy_password = form.cleaned_data["proxy_password"]
            if proxy_password:
                config.proxy_secret = cipher().encrypt(proxy_password.encode()).decode()
            config.save()
            audit(
                request.user,
                "ai_settings_updated",
                config,
                provider=config.provider,
                model=config.model,
                key_replaced=bool(key),
                proxy_enabled=config.proxy_enabled,
                proxy_host=urlsplit(config.proxy_url).hostname if config.proxy_url else "",
                proxy_password_replaced=bool(proxy_password),
            )

        if action == "test":
            try:
                key = api_key(config.provider)
                if not key:
                    messages.error(request, "Сначала сохраните API-ключ выбранного провайдера.")
                else:
                    result = check_provider_connection(config.provider, key)
                    route = "через прокси" if result.get("via_proxy") else "напрямую"
                    if result["ok"]:
                        messages.success(request, f'{result["message"]} Маршрут: {route}. Домен: {result["host"]}.')
                    else:
                        messages.error(request, f'{result["message"]} Маршрут: {route}.')
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            return redirect("ai_settings")

        messages.success(request, "Сохранено. Модель применяется к новым конкурсам и песочнице; настройки прокси — ко всем новым API-запросам.")
        return redirect("ai_settings")

    provider = (request.POST.get("provider") if request.method == "POST" else profile["provider"]) or "openrouter"
    check_host = urlsplit(CHECK_ENDPOINTS.get(provider, "")).hostname or ""
    return render(request, "trainer/ai_settings.html", {
        "nav": "ai",
        "form": form,
        "key_present": profile_has_key(profile),
        "proxy_password_present": bool(config and config.proxy_secret),
        "check_host": check_host,
    })
