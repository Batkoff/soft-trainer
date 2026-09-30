"""Диаграммы получают уже ограниченный правами и конкурсом набор попыток."""
from datetime import timedelta
from django.db.models import Avg, Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from .models import SKILLS


def dashboard_data(graded, chart_days=7):
    today = timezone.localdate()
    if chart_days not in (7, 14, 30):
        chart_days = 7
    first_day = today - timedelta(days=chart_days - 1)
    recent = graded.filter(graded_at__date__gte=first_day, graded_at__date__lte=today)
    aggregates = {row["day"]: row for row in recent.annotate(day=TruncDate("graded_at"))
        .values("day").annotate(average=Avg("score"), count=Count("pk")).order_by("day")}
    days = []
    for offset in range(chart_days):
        day = first_day + timedelta(days=offset)
        row = aggregates.get(day)
        average = round(row["average"]) if row else None
        days.append({"label": day.strftime("%d.%m"), "average": average,
                     "count": row["count"] if row else 0, "height": max(2, average or 0)})
    total = graded.count()
    bands = []
    for low, high, label, tone in [(0, 49, "0–49 · нужна поддержка", "low"), (50, 69, "50–69 · есть над чем работать", "medium"),
                                  (70, 84, "70–84 · хороший ответ", "good"), (85, 100, "85–100 · отличный ответ", "great")]:
        count = graded.filter(score__gte=low, score__lte=high).count()
        bands.append({"label": label, "count": count, "percent": round(count*100/total) if total else 0, "tone": tone})
    values = {key: [] for key in SKILLS}
    # Только необходимые JSON-поля, без текстов ответов, фото и запросов к API.
    for reviewed, payload in recent.values_list("reviewed_skills", "evaluation__payload").iterator():
        payload = payload or {}
        skills = reviewed or payload.get("skills", {})
        scale = 100 if reviewed else payload.get("skill_scale", 4)
        if not isinstance(scale, (int, float)) or scale <= 0:
            continue
        for key in SKILLS:
            value = skills.get(key)
            if isinstance(value, (int, float)) and 0 <= value <= scale:
                values[key].append(value * 100 / scale)
    skills = [{"name": name, "average": round(sum(values[key])/len(values[key])) if values[key] else None,
               "count": len(values[key])} for key, name in SKILLS.items()]
    return {"days": days, "bands": bands, "skills": skills, "total": total, "chart_days": chart_days,
            "recent_count": sum(day["count"] for day in days)}
