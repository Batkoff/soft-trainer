"""Настройки подключения доступны только владельцу проекта."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from .ai_configuration import cipher
from .evaluation_profiles import current_profile, profile_has_key
from .forms import AIConfigurationForm
from .models import AIConfiguration
from .services import audit


@sensitive_post_parameters("api_key")
@login_required
@require_http_methods(["GET", "POST"])
def ai_settings(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    profile = current_profile()
    choices = dict(AIConfigurationForm.base_fields["model_choice"].choices)
    form = AIConfigurationForm(request.POST or None, initial={
        "provider": profile["provider"],
        "model_choice": profile["model"] if profile["model"] in choices else "custom",
        "custom_model": profile["model"] if profile["model"] not in choices else ""})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            AIConfiguration.objects.get_or_create(pk=1, defaults={"model": profile["model"]})
            config = AIConfiguration.objects.select_for_update().get(pk=1)
            config.provider = form.cleaned_data["provider"]
            config.model = form.cleaned_data["model"]
            key = form.cleaned_data["api_key"]
            if key:
                setattr(config, config.provider + "_secret", cipher().encrypt(key.encode()).decode())
            config.save()
            audit(request.user, "ai_settings_updated", config, provider=config.provider,
                  model=config.model, key_replaced=bool(key))
        messages.success(request, "Сохранено. Настройки применяются к новым конкурсам и песочнице. Модель начатых конкурсов не меняется.")
        return redirect("ai_settings")
    return render(request, "trainer/ai_settings.html", {"form": form, "key_present": profile_has_key(profile)})
