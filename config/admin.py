"""Навигация администратора: рабочие разделы отдельно от диагностики."""
from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig


class TonAdminSite(AdminSite):
    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def admin_view(self, view, cacheable=False):
        # Авторизованному руководителю даём 403, а не круговой переход на вход.
        from functools import wraps
        from django.core.exceptions import PermissionDenied
        wrapped = super().admin_view(view, cacheable)
        @wraps(view)
        def restricted(request, *args, **kwargs):
            if request.user.is_authenticated and not self.has_permission(request):
                raise PermissionDenied
            return wrapped(request, *args, **kwargs)
        return restricted

    def get_app_list(self, request, app_label=None):
        apps = super().get_app_list(request, app_label)
        labels = {
            "Evaluation": "Результаты проверки", "CalibrationCase": "Эталонные ответы",
            "CalibrationRun": "Проверки эталонов", "Attempt": "Ответы сотрудников",
            "EvaluationTrace": "Запросы к нейросети"}
        # Очередью управляют сервисы приложения. Её внутренние таблицы не нужны оператору.
        for app in apps:
            for model in app["models"]:
                model["name"] = labels.get(model["object_name"], model["name"])
                model["advanced"] = model["object_name"] in {"Evaluation", "CalibrationCase", "CalibrationRun", "Group"}
        return [app for app in apps if app["app_label"] != "procrastinate"]


class TonAdminConfig(AdminConfig):
    default_site = "config.admin.TonAdminSite"
