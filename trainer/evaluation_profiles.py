"""Профиль сохраняется в конкурсе и попытке отдельно от секретов подключения."""
from django.conf import settings

PROMPT_VERSION = "soft-v2"
FREE_OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

def current_profile():
    profile = {"provider": settings.EVALUATOR_BACKEND, "model": settings.EVALUATOR_MODEL,
               "prompt_version": PROMPT_VERSION}
    if not getattr(settings, "ALLOW_TEST_EVALUATOR", False):
        from hashlib import sha256
        from .models import EvaluationPrompt
        from .evaluation_prompt import SYSTEM_PROMPT
        saved_prompt = EvaluationPrompt.objects.filter(pk=1).first()
        text = saved_prompt.text if saved_prompt else SYSTEM_PROMPT
        profile["prompt_text"] = text
        profile["prompt_hash"] = sha256(text.encode("utf-8")).hexdigest()
        from .ai_configuration import configuration
        config = configuration()
        if config:
            profile.update(provider=config.provider, model=config.model)
        elif profile["provider"] == "demo":
            profile.update(provider="openrouter", model=FREE_OPENROUTER_MODEL)
    if profile["provider"] == "openrouter" and profile["model"] == FREE_OPENROUTER_MODEL:
        # У Nemotron thinking включён по умолчанию. Оставляем бюджет на сам JSON-разбор.
        # Параметр сохраняется вместе с моделью, чтобы правила активного конкурса не менялись.
        profile["reasoning_enabled"] = False
    return profile

def profile_for_rubric(rubric):
    # Старые конкурсы до подключения API всегда остаются демонстрационными.
    if "evaluator" not in rubric and rubric.get("version") == "demo-v1":
        return {"provider": "demo", "model": "demo-v1", "prompt_version": PROMPT_VERSION}
    return rubric.get("evaluator", {})

def profile_has_key(profile):
    from .ai_configuration import api_key
    from django.core.exceptions import ValidationError
    try:
        return bool(api_key(profile.get("provider")))
    except ValidationError:
        return False
