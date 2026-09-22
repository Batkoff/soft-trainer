"""Общий контракт оценщика и расчёт баллов; сетевой адаптер вынесен отдельно."""
import time
import math
from dataclasses import dataclass
from typing import Protocol
from django.conf import settings
from .models import SKILLS

class TemporaryEvaluationError(Exception):
    def __init__(self, message, *, metadata=None):
        super().__init__(message)
        # Детали валидации остаются в закрытом trace, а не в сообщении сотруднику.
        self.metadata = metadata or {}

class PermanentEvaluationError(Exception):
    pass

@dataclass(frozen=True)
class EvaluationInput:
    assignment: dict
    answer: str
    rubric: dict
    scenario: str = "normal"
    try_number: int = 1
    attempt_id: str = ""

class Evaluator(Protocol):
    def evaluate(self, data: EvaluationInput) -> dict: ...

class DemoEvaluator:
    """Фиксированные тестовые баллы проверяют механику, НЕ качество текста."""
    def evaluate(self, data: EvaluationInput) -> dict:
        time.sleep(settings.DEMO_EVALUATION_DELAY)
        if data.scenario == "transient" and data.try_number == 1:
            raise TemporaryEvaluationError("Демонстрация временного сбоя")
        if data.scenario == "failure":
            raise PermanentEvaluationError("Демонстрация постоянного сбоя")
        scores = {"clarity": 4, "tone": 3, "empathy": 3, "expectations": 3, "initiative": 3}
        return {"schema_version": 1, "is_demo": True, "model": "demo-v1", "usage": {"input_tokens": 0, "output_tokens": 0},
            "hard_verdict": "violated" if data.scenario == "hard_error" else "unverified",
            "skills": scores,
            "strengths": ["Ответ сохранён, очередь обработана, результат доступен сотруднику."],
            "improvements": ["Это тестовый результат. Анализ содержания и рекомендации появятся после подключения модели."],
            "hard_notes": ["Симуляция ошибки hard-части."] if data.scenario == "hard_error" else ["Содержание ответа пока не проверяется."],
            "improved_answer": ""}

def get_evaluator(rubric: dict | None = None) -> Evaluator:
    from .evaluation_profiles import current_profile, profile_for_rubric
    profile = current_profile() if rubric is None else profile_for_rubric(rubric)
    if profile.get("provider") == "demo":
        if getattr(settings, "ALLOW_TEST_EVALUATOR", False):
            return DemoEvaluator()
        raise PermanentEvaluationError("Тестовый оценщик отключён. Создайте конкурс с реальной моделью.")
    if profile.get("provider") in ("openai", "openrouter"):
        from .evaluation_http import LiveEvaluator
        return LiveEvaluator(profile)
    raise PermanentEvaluationError("Неизвестный профиль оценщика. Проверьте настройки конкурса.")

def calculate_score(payload: dict, answer: str, rubric: dict) -> int | None:
    """Баллы вычисляет приложение. Невалидный ответ оценщика не становится нулём сотрудника."""
    if payload.get("hard_verdict") not in ("passed", "violated", "unverified", "uncertain"):
        raise PermanentEvaluationError("Неверный формат результата")
    skills = payload.get("skills", {})
    scale = payload.get("skill_scale", 4)  # Старые демонстрационные оценки остаются совместимыми.
    if type(scale) is not int or scale not in (4, 100):
        raise PermanentEvaluationError("Неверная шкала навыков")
    if set(skills) != set(SKILLS) or any(type(value) is not int or not 0 <= value <= scale for value in skills.values()):
        raise PermanentEvaluationError("Неверная шкала навыков")
    weights = rubric.get("weights", {})
    if set(weights) != set(SKILLS) or any(type(w) not in (int, float) or not math.isfinite(w) or w < 0 for w in weights.values()) or sum(weights.values()) <= 0:
        raise PermanentEvaluationError("Неверные веса критериев")
    if payload["hard_verdict"] == "violated" or not answer.strip():
        return 0
    if payload["hard_verdict"] == "uncertain":
        return None
    return round(100 * sum(skills[key]*weights[key] for key in SKILLS) / (scale*sum(weights.values())))
