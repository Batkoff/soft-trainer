"""Профиль сохраняется в конкурсе и попытке. Ключи остаются только в окружении."""
from django.conf import settings

PROMPT_VERSION = "soft-v1"
FREE_OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

def current_profile():
    profile = {"provider": settings.EVALUATOR_BACKEND, "model": settings.EVALUATOR_MODEL,
               "prompt_version": PROMPT_VERSION}
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
    return bool({"openai": settings.OPENAI_API_KEY,
                 "openrouter": settings.OPENROUTER_API_KEY}.get(profile.get("provider")))
