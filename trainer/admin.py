"""Стандартная админка Django: минимум собственного кода для управления."""
from django.contrib import admin, messages
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.utils.html import format_html
from django.urls import reverse, path
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.views.decorators.http import require_POST, require_http_methods
from .admin_forms import ContestAdminForm
from .models import (Attempt, AuditEvent, CalibrationCase, CalibrationRun, Contest, Evaluation,
                     EvaluationTrace, Exercise)
from . import services
from . import diagnostics

admin.site.site_header = "Тренажёр · управление"
admin.site.site_title = "Управление тренажёром"
admin.site.index_title = "Задания, конкурсы и проверка"

class StatusAdminMixin:
    @admin.display(description="Статус", ordering="status")
    def status_badge(self, obj):
        return format_html('<span class="admin-status admin-status-{}">{}</span>', obj.status, obj.get_status_display())

class AuditAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        services.audit(request.user, "admin_updated" if change else "admin_created", obj,
            model=obj._meta.label, fields=list(form.changed_data))

    def has_delete_permission(self, request, obj=None):
        return False  # Исторические данные архивируем, не удаляем каскадом.

@admin.register(Exercise)
class ExerciseAdmin(StatusAdminMixin, AuditAdmin):
    change_form_template = "admin/exercise_change.html"
    list_display = ("title", "category", "status_badge", "version")
    list_filter = ("status", "category")
    search_fields = ("title", "hard_answer")
    readonly_fields = ("version", "created_at", "updated_at")
    actions = ("publish", "archive", "new_version")
    fieldsets = (
        ("О задании", {"fields": ("title", "category", ("status", "version"))}),
        ("Что увидит сотрудник", {"fields": ("customer_message", "hard_answer")}),
        ("Условия проверки", {"fields": ("required_facts", "allowed_actions", "forbidden_promises"),
            "description": "Эти поля доступны администраторам. Сотрудник сам выбирает формулировку ответа."}),
        ("История версии", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != Exercise.Status.DRAFT:
            return tuple(field.name for field in Exercise._meta.fields if field.name != "id")
        return self.readonly_fields + ("status",)

    @admin.action(description="Опубликовать выбранные черновики")
    def publish(self, request, queryset):
        for item in queryset.filter(status=Exercise.Status.DRAFT):
            item.full_clean()
            item.status = Exercise.Status.PUBLISHED
            item.save(update_fields=["status"])
            services.audit(request.user, "exercise_published", item)

    @admin.action(description="Архивировать выбранные задания")
    def archive(self, request, queryset):
        for item in queryset:
            if item.contest_set.filter(status=Contest.Status.ACTIVE).exists():
                self.message_user(request, f"{item.title}: используется в запущенном конкурсе.", messages.ERROR)
                continue
            item.status = Exercise.Status.ARCHIVED
            item.save(update_fields=["status"])
            services.audit(request.user, "exercise_archived", item)

    @admin.action(description="Создать новую версию для редактирования")
    def new_version(self, request, queryset):
        for item in queryset:
            original_id = item.pk
            item.pk = None
            item.version += 1
            item.status = Exercise.Status.DRAFT
            item.save()
            services.audit(request.user, "exercise_version_created", item, previous_id=original_id)
        self.message_user(request, "Новые версии появились в списке как черновики.")

    def get_urls(self):
        return [path("<int:object_id>/new-version/", self.admin_site.admin_view(require_POST(self.edit_version)),
                     name="trainer_exercise_new_version")] + super().get_urls()

    @transaction.atomic
    def edit_version(self, request, object_id):
        item = get_object_or_404(Exercise.objects.select_for_update(), pk=object_id)
        if not self.has_add_permission(request) or not self.has_change_permission(request, item):
            raise PermissionDenied
        if item.status != Exercise.Status.DRAFT:
            previous_id = item.pk
            item.pk = None
            item.version += 1
            item.status = Exercise.Status.DRAFT
            item.save()
            services.audit(request.user, "exercise_version_created", item, previous_id=previous_id)
            self.message_user(request, "Открыт редактируемый черновик. Сохраните изменения и опубликуйте его в списке заданий.")
        return redirect("admin:trainer_exercise_change", item.pk)

@admin.register(Contest)
class ContestAdmin(StatusAdminMixin, AuditAdmin):
    form = ContestAdminForm
    change_form_template = "admin/contest_change.html"
    list_display = ("title", "phase_badge", "starts_at", "ends_at", "daily_limit", "time_limit_seconds")
    list_filter = ("status",)
    search_fields = ("title",)
    filter_horizontal = ("participants", "exercises")
    readonly_fields = ("status", "rubric", "final_standings", "finalized_at", "closed_at")
    actions = ("activate", "finalize")
    fieldsets = (
        ("Конкурс и сроки", {"fields": ("title", "status", "starts_at", "ends_at", "closed_at")}),
        ("Правила тренировки", {"fields": ("daily_limit", "time_limit_seconds"),
            "description": "После запуска условия конкурса фиксируются."}),
        ("Задания и участники", {"fields": ("exercises", "participants")}),
        ("Призовой фонд · T-Money", {"fields": ("first_prize", "second_prize", "third_prize")}),
        ("Оценка и итоговый рейтинг", {"fields": ("rubric", "final_standings", "finalized_at"), "classes": ("collapse",)}),
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status != Contest.Status.DRAFT:
            return tuple(field.name for field in Contest._meta.fields if field.name != "id") + ("participants", "exercises")
        return self.readonly_fields

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "exercises":
            kwargs["queryset"] = Exercise.objects.filter(status="published")
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    @admin.display(description="Состояние")
    def phase_badge(self, obj):
        return format_html('<span class="admin-status admin-status-{}">{}</span>', obj.phase, obj.phase_label)

    def get_urls(self):
        return [path("<int:object_id>/close/",
            self.admin_site.admin_view(require_http_methods(["GET", "POST"])(self.close_early)),
            name="trainer_contest_close")] + super().get_urls()

    def close_early(self, request, object_id):
        if not request.user.is_superuser:
            raise PermissionDenied
        contest = get_object_or_404(Contest, pk=object_id)
        if request.method == "POST":
            try:
                contest = services.close_contest_early(request.user, contest.pk)
                if contest.status == Contest.Status.FINISHED:
                    self.message_user(request, "Итоги зафиксированы и перенесены в архив. На главной выбран следующий доступный конкурс.")
                else:
                    self.message_user(request, "Приём ответов завершён. Черновики отправлены на проверку. Итоги зафиксируются автоматически после всех проверок и пересмотров.")
            except ValidationError as exc:
                self.message_user(request, "; ".join(exc.messages), messages.ERROR)
            return redirect("admin:trainer_contest_change", contest.pk)
        return TemplateResponse(request, "admin/contest_close.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta,
            "title": "Завершить конкурс", "contest": contest,
            "writing_count": contest.attempt_set.filter(status=Attempt.Status.WRITING).count(),
            "pending_count": contest.attempt_set.exclude(status__in=[Attempt.Status.WRITING, Attempt.Status.GRADED]).count(),
        })

    @admin.action(description="Запустить выбранные конкурсы")
    def activate(self, request, queryset):
        for item in queryset:
            try:
                services.activate_contest(request.user, item.pk)
                self.message_user(request, f"{item.title}: запущен.")
            except ValidationError as exc:
                self.message_user(request, "; ".join(exc.messages), messages.ERROR)

    @admin.action(description="Зафиксировать итоговый рейтинг")
    def finalize(self, request, queryset):
        for item in queryset:
            try:
                services.finalize_contest(request.user, item.pk)
                self.message_user(request, "Итоги зафиксированы. При равенстве баллов — общее место.")
            except ValidationError as exc:
                self.message_user(request, "; ".join(exc.messages), messages.ERROR)

class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False


class LogAdmin(ReadOnlyAdmin):
    """Текстовый экспорт карточки, отмеченных строк или текущего фильтра."""
    change_list_template = "admin/log_list.html"
    actions = ("export_txt",)

    def get_urls(self):
        name = self.model._meta.model_name
        return [
            path("export/", self.admin_site.admin_view(self.export_filtered), name=f"trainer_{name}_export"),
            path("<int:object_id>/text/", self.admin_site.admin_view(self.download_text), name=f"trainer_{name}_text"),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, extra_context={**(extra_context or {}),
            "download_all_url": reverse(f"admin:trainer_{self.model._meta.model_name}_export")})

    def export_filtered(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        queryset = self.get_changelist_instance(request).get_queryset(request)
        return self.text_response(request, queryset)

    def download_text(self, request, object_id):
        obj = get_object_or_404(self.get_queryset(request), pk=object_id)
        if not self.has_view_permission(request, obj):
            raise PermissionDenied
        return self.text_response(request, self.get_queryset(request).filter(pk=obj.pk))

    @admin.action(description="Скачать выбранные записи (.txt)", permissions=["view"])
    def export_txt(self, request, queryset):
        if not self.has_view_permission(request):
            raise PermissionDenied
        return self.text_response(request, queryset)

    def text_response(self, request, queryset):
        def chunks():
            # BOM помогает Блокноту Windows правильно определить UTF-8.
            yield "\ufeff" + diagnostics.export_header()
            for obj in queryset.iterator(chunk_size=100):
                yield self.export_record(request, obj)
        response = StreamingHttpResponse(chunks(), content_type="text/plain; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{self.model._meta.model_name}-logs.txt"'
        response["Cache-Control"] = "no-store"
        return response

    @admin.display(description="")
    def download_link(self, obj):
        return format_html('<a class="admin-download-link" href="{}">TXT ↓</a>',
            reverse(f"admin:trainer_{self.model._meta.model_name}_text", args=[obj.pk]))

@admin.register(Attempt)
class AttemptAdmin(StatusAdminMixin, ReadOnlyAdmin):
    list_display = ("employee", "exercise", "mode", "status_badge", "score", "timed_out", "open_result")
    list_filter = ("mode", "status", "contest", "timed_out")
    search_fields = ("user__username", "user__first_name", "user__last_name", "exercise__title")
    list_select_related = ("user", "exercise")
    @admin.display(description="Сотрудник", ordering="user__username")
    def employee(self, obj):
        return format_html('{}<span class="admin-employee-login">@{}</span>',
            obj.user.get_full_name() or obj.user.username, obj.user.username)

    @admin.display(description="Результат")
    def open_result(self, obj):
        return format_html('<a href="{}">Посмотреть работу →</a>', reverse("attempt", args=[obj.pk]))

@admin.register(AuditEvent)
class AuditEventAdmin(LogAdmin):
    list_display = ("created_at", "event_name", "status_badge", "actor_label", "object_id", "download_link")
    list_filter = ("action",)
    search_fields = ("object_id", "actor__username", "action")
    list_select_related = ("actor",)
    readonly_fields = ("created_at", "event_name", "action", "status_badge", "actor_label", "object_id",
                       "diagnostic_link", "pretty_details", "download_link")
    fields = readonly_fields

    @admin.display(description="Событие", ordering="action")
    def event_name(self, obj):
        return diagnostics.event_title(obj)

    @admin.display(description="Кто")
    def actor_label(self, obj):
        return obj.actor or "Система (таймер / очередь)"

    @admin.display(description="Результат события")
    def status_badge(self, obj):
        state, label = diagnostics.event_state(obj)
        return format_html('<span class="admin-status admin-status-{}">{}</span>', state, label)

    @admin.display(description="Диагностика")
    def diagnostic_link(self, obj):
        trace = EvaluationTrace.objects.select_related("attempt").filter(pk=obj.details.get("trace_id")).first()
        if trace:
            return format_html('<a href="{}">Открыть запрос #{}</a><p>{}</p><p>{}</p>',
                reverse("admin:trainer_evaluationtrace_change", args=[trace.pk]), trace.pk,
                diagnostics.error_reason(trace.error_message), diagnostics.current_state(trace.attempt))
        return "У этого события нет запроса к модели."

    @admin.display(description="Данные события")
    def pretty_details(self, obj):
        return format_html('<pre class="diagnostic-json">{}</pre>', diagnostics.pretty(obj.details))

    def export_record(self, request, obj):
        result = diagnostics.event_text(obj)
        if request.user.has_perm("trainer.view_evaluationtrace"):
            trace = EvaluationTrace.objects.select_related("attempt", "attempt__user").filter(pk=obj.details.get("trace_id")).first()
            if trace:
                result += diagnostics.trace_text(trace)
        return result


@admin.register(Evaluation)
class EvaluationAdmin(ReadOnlyAdmin):
    """Нормализованный результат, который видит сотрудник."""
    list_display = ("attempt", "backend", "duration_ms", "created_at")
    list_filter = ("backend",)
    search_fields = ("attempt__user__username", "attempt__id")
    list_select_related = ("attempt", "attempt__user")
    readonly_fields = tuple(field.name for field in Evaluation._meta.fields)


@admin.register(EvaluationTrace)
class EvaluationTraceAdmin(LogAdmin):
    """Техническая диагностика prompt → provider response по каждой попытке."""
    list_display = ("created_at", "attempt_short", "try_number", "provider", "model", "status_badge",
                    "duration_ms", "token_summary", "download_link")
    list_filter = ("status", "provider", "prompt_version")
    search_fields = ("attempt__user__username", "attempt__id", "model", "error_type", "error_message")
    list_select_related = ("attempt", "attempt__user")
    readonly_fields = ("created_at", "attempt_short", "status_badge", "try_number", "provider", "model",
        "prompt_version", "duration_ms", "token_summary", "download_link", "current_status",
        "error_type", "error_description", "metadata_pretty", "prompt_pretty", "response_pretty",
        "request_pretty", "normalized_pretty", "attempt_history")
    fieldsets = (
        ("Результат запроса", {"fields": ("status_badge", "current_status", "attempt_short", "attempt_history",
            "created_at", "try_number", "provider", "model", "prompt_version", "duration_ms", "token_summary", "download_link")}),
        ("Ошибка", {"fields": ("error_type", "error_description", "metadata_pretty")}),
        ("Промпт и задание", {"fields": ("prompt_pretty",)}),
        ("Ответ провайдера", {"fields": ("response_pretty",)}),
        ("Результат для сотрудника", {"fields": ("normalized_pretty",)}),
        ("Полный запрос, включая схему JSON", {"fields": ("request_pretty",), "classes": ("collapse",)}),
    )

    @admin.display(description="Статус", ordering="status")
    def status_badge(self, obj):
        return format_html('<span class="admin-status admin-status-{}">{}</span>', obj.status, obj.get_status_display())

    @admin.display(description="Попытка")
    def attempt_short(self, obj):
        return format_html('<a href="{}">@{} · {}… → работа</a>', reverse("attempt", args=[obj.attempt_id]),
                           obj.attempt.user.username, str(obj.attempt_id)[:8])

    @admin.display(description="Вся история проверки")
    def attempt_history(self, obj):
        return format_html('<a href="{}?attempt__id__exact={}">Все запросы по этой работе →</a>',
            reverse("admin:trainer_evaluationtrace_changelist"), obj.attempt_id)

    @admin.display(description="Состояние работы сейчас")
    def current_status(self, obj):
        return diagnostics.current_state(obj.attempt)

    @admin.display(description="Причина")
    def error_description(self, obj):
        return diagnostics.error_reason(obj.error_message) or "Нет ошибки."

    @admin.display(description="Код ошибки / HTTP")
    def metadata_pretty(self, obj):
        return format_html('<pre class="diagnostic-json">{}</pre>', diagnostics.pretty(obj.error_metadata))

    @admin.display(description="Отправленные инструкции")
    def prompt_pretty(self, obj):
        return format_html('<pre class="diagnostic-json">{}</pre>', diagnostics.prompt_text(obj.request_payload))

    @admin.display(description="Полный ответ")
    def response_pretty(self, obj):
        return format_html('<pre class="diagnostic-json">{}</pre>', diagnostics.response_text(obj.response_payload))

    @admin.display(description="JSON-запрос")
    def request_pretty(self, obj):
        return format_html('<pre class="diagnostic-json">{}</pre>', diagnostics.pretty(obj.request_payload))

    @admin.display(description="Проверенный разбор")
    def normalized_pretty(self, obj):
        return format_html('<pre class="diagnostic-json">{}</pre>', diagnostics.pretty(obj.normalized_payload))

    def export_record(self, request, obj):
        return diagnostics.trace_text(obj)

    @admin.display(description="Токены")
    def token_summary(self, obj):
        return f"{obj.input_tokens or 0} / {obj.output_tokens or 0}"

    @admin.display(description="")
    def download_link(self, obj):
        txt_url = reverse(f"admin:trainer_{self.model._meta.model_name}_text", args=[obj.pk])
        json_url = reverse("admin:trainer_evaluationtrace_download", args=[obj.pk])
        return format_html('<span class="admin-downloads"><a href="{}">TXT ↓</a><a href="{}">JSON ↓</a></span>',
                           txt_url, json_url)

    def get_urls(self):
        custom = [path("<int:object_id>/download/", self.admin_site.admin_view(self.download),
                      name="trainer_evaluationtrace_download")]
        return custom + super().get_urls()

    def download(self, request, object_id):
        trace = get_object_or_404(EvaluationTrace.objects.select_related("attempt", "attempt__user"), pk=object_id)
        if not self.has_view_permission(request, trace):
            raise PermissionDenied
        response = JsonResponse(diagnostics.trace_payload(trace), json_dumps_params={"ensure_ascii": False, "indent": 2})
        response["Content-Disposition"] = f'attachment; filename="evaluation-trace-{trace.pk}.json"'
        response["Cache-Control"] = "no-store"
        return response

@admin.register(CalibrationCase)
class CalibrationCaseAdmin(AuditAdmin):
    list_display = ("title", "exercise", "expected_min", "expected_max", "expected_hard")
    actions = ("run_cases",)

    def get_form(self, request, obj=None, **kwargs):
        from django.conf import settings
        form = super().get_form(request, obj, **kwargs)
        if not getattr(settings, "ALLOW_TEST_EVALUATOR", False) and "scenario" in form.base_fields:
            form.base_fields["scenario"].choices = [("real", "Оценка нейросетью")]
            form.base_fields["scenario"].initial = "real"
        return form

    @admin.action(description="Запустить выбранные примеры калибровки")
    def run_cases(self, request, queryset):
        started = 0
        for case in queryset:
            try:
                with transaction.atomic():
                    attempt = services.start_sandbox(request.user, case.exercise, case.scenario, case.answer)
                    CalibrationRun.objects.create(case=case, attempt=attempt, expected_min=case.expected_min,
                        expected_max=case.expected_max, expected_hard=case.expected_hard)
                    started += 1
            except ValidationError as exc:
                self.message_user(request, f"{case.title}: {'; '.join(exc.messages)}", messages.ERROR)
        if started:
            self.message_user(request, f"В очереди: {started}. Результаты — в «Проверки эталонов» и аналитике.")

@admin.register(CalibrationRun)
class CalibrationRunAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "case", "result", "open_attempt")
    list_select_related = ("case", "attempt")
    @admin.display(description="Совпадение с ожиданием")
    def result(self, obj):
        if obj.attempt.status != Attempt.Status.GRADED:
            return obj.attempt.get_status_display()
        return "Совпало" if obj.expected_min <= obj.attempt.score <= obj.expected_max and obj.expected_hard == obj.attempt.hard_verdict else "Расхождение"
    @admin.display(description="Ответ")
    def open_attempt(self, obj):
        return format_html('<a href="{}">Посмотреть</a>', reverse("attempt", args=[obj.attempt_id]))

# Регистрация простой карточки сотрудника после стандартного django.contrib.auth.
from . import user_admin  # noqa: E402,F401


from .models import EvaluationPrompt


@admin.register(EvaluationPrompt)
class EvaluationPromptAdmin(AuditAdmin):
    fieldsets = (("Правила оценки", {
        "fields": ("version", "text", "updated_at"),
        "description": "Изменения действуют для новых конкурсов и новых попыток песочницы. Активные конкурсы и повторы проверки используют сохранённую копию. При изменении текста версия повышается автоматически. Не вставляйте API-ключи: промпт передаётся модели и сохраняется в журнале запросов.",
    }),)
    readonly_fields = ("version", "updated_at")
    list_display = ("__str__", "version_label", "updated_at")

    @admin.display(description="Версия", ordering="version")
    def version_label(self, obj):
        return f"soft-v{obj.version}"

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser and not EvaluationPrompt.objects.exists()

    def save_model(self, request, obj, form, change):
        obj.pk = 1
        if change and "text" in form.changed_data:
            previous = EvaluationPrompt.objects.filter(pk=1).only("version").first()
            obj.version = (previous.version if previous else obj.version) + 1
        super().save_model(request, obj, form, change)
        messages.info(request, f"Промпт сохранён как soft-v{obj.version}. Новые конкурсы и попытки песочницы получат эти правила. Действующие конкурсы сохраняют прежний промпт.")
