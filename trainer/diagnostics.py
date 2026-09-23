"""Читаемые журналы. Исходные записи неизменны, представление и выгрузка общие."""
import json
from django.utils import timezone
from .releases import VERSION

EVENT_TITLES = {
    "team_updated": "Изменён состав команды", "team_assignment": "Назначен руководитель",
    "user_created": "Пользователь создан", "user_updated": "Пользователь изменён",
    "ai_settings_updated": "Изменены настройки нейросети",
    "admin_access_recovered": "Восстановлен доступ администратора",
    "attempt_started": "Задание открыто", "attempt_submitted": "Ответ отправлен на проверку",
    "evaluation_completed": "Оценка получена", "evaluation_error": "Ошибка проверки",
    "evaluation_requeued": "Проверка запущена повторно администратором",
    "manual_review": "Оценка пересмотрена", "sandbox_started": "Запущена песочница",
    "contest_activated": "Конкурс запущен", "contest_closed_early": "Приём завершён досрочно",
    "contest_finalized": "Итоги зафиксированы", "contest_auto_finalized": "Итоги зафиксированы автоматически",
    "exercise_published": "Задание опубликовано", "exercise_archived": "Задание архивировано",
    "exercise_version_created": "Создана новая версия задания", "profile_updated": "Профиль обновлён",
    "admin_created": "Объект создан", "admin_updated": "Объект изменён",
}


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def event_title(event):
    return EVENT_TITLES.get(event.action, event.action)


def event_state(event):
    if event.action == "evaluation_error":
        return ("retry", "Сбой · назначен повтор") if event.details.get("status") == "retry" else ("failed", "Ошибка · нужен администратор")
    if event.details.get("status") == "review":
        return "review", "Нужен пересмотр"
    if event.action in ("attempt_submitted", "evaluation_requeued"):
        return "queued", "В очередь"
    return "success", "Успешно"


def error_reason(message):
    # Старые логи тоже отображаются без обещания повтора, который уже состоялся.
    return message.replace(" Проверка повторится.", "").replace(" Нужен пересмотр.", "")


def current_state(attempt):
    labels = {"retry": "Ожидает автоматического повтора в очереди.",
              "queued": "Ожидает проверки в очереди.", "evaluating": "Модель проверяет ответ.",
              "graded": "Проверка завершена. Автоматических повторов больше нет.",
              "review": "Автоматическая проверка остановлена. Откройте работу: доступен пересмотр или повтор администратором.",
              "writing": "Сотрудник ещё пишет ответ."}
    return labels.get(attempt.status, attempt.get_status_display())


def trace_payload(trace):
    return {"trace_id": trace.pk, "created_at": trace.created_at.isoformat(),
        "attempt_id": str(trace.attempt_id), "employee": trace.attempt.user.username,
        "attempt_status_now": trace.attempt.status, "try_number": trace.try_number,
        "provider": trace.provider, "model": trace.model, "prompt_version": trace.prompt_version,
        "status": trace.status, "duration_ms": trace.duration_ms,
        "tokens": {"input": trace.input_tokens, "output": trace.output_tokens},
        "error": {"type": trace.error_type, "message": trace.error_message, "metadata": trace.error_metadata},
        "request_payload": trace.request_payload, "response_payload": trace.response_payload,
        "normalized_payload": trace.normalized_payload}


def prompt_text(payload):
    parts = []
    for message in payload.get("messages", []):
        content = message.get("content", "")
        try:
            content = pretty(json.loads(content))
        except (ValueError, TypeError):
            pass
        parts.append(f"[{message.get('role', '')}]\n{content}")
    return "\n\n".join(parts) or "HTTP-запрос не отправлялся (локальное правило или демонстрационный сценарий)."


def response_text(payload):
    """Развернуть JSON внутри message.content, сохраняя оригинальный trace."""
    response = dict(payload)
    choices = response.get("choices")
    if isinstance(choices, list):
        expanded = []
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                expanded.append(choice)
                continue
            message = dict(choice["message"])
            try:
                message["content"] = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                pass
            expanded.append({**choice, "message": message})
        response["choices"] = expanded
    return pretty(response)


def export_header():
    return f"Тон · @batkoff · v{VERSION} · диагностический отчёт\nВыгружено: {timezone.localtime():%d.%m.%Y %H:%M:%S} МСК\n\n"


def event_text(event):
    actor = event.actor.username if event.actor else "Система (таймер / очередь)"
    return (f"Событие #{event.pk} · {timezone.localtime(event.created_at):%d.%m.%Y %H:%M:%S} МСК\n"
            f"{event_title(event)} [{event.action}] · {event_state(event)[1]}\n"
            f"Кто: {actor}\nОбъект: {event.object_id}\nДанные:\n{pretty(event.details)}\n\n")


def trace_text(trace):
    return (f"ПРОВЕРКА #{trace.pk} · запрос {trace.try_number} · {trace.get_status_display()}\n"
            f"Состояние работы сейчас: {current_state(trace.attempt)}\n"
            f"Причина: {error_reason(trace.error_message) or '—'}\n"
            f"ПРОМПТ И ЗАДАНИЕ\n{prompt_text(trace.request_payload)}\n\n"
            f"ОТВЕТ ПРОВАЙДЕРА\n{response_text(trace.response_payload)}\n\n"
            f"ПОЛНЫЕ ДАННЫЕ ЗАПРОСА И ОТВЕТА\n{pretty(trace_payload(trace))}\n\n")
