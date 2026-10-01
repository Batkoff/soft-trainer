from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms.models import BaseInlineFormSet
from django.urls import reverse
from django.utils.html import format_html
from trainer.admin import AuditAdmin
from .models import Scenario, Topic, DialoguePrompt, Session, DialogueTrace


class TopicFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [f.cleaned_data for f in self.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        codes = {row.get('code') for row in rows}
        if not 1 <= len(rows) <= 10 or self.instance.first_topic not in codes:
            raise ValidationError('Нужны 1–10 hard-блоков, включая код первой темы сценария.')
        if len(codes) != len(rows):
            raise ValidationError('Коды тем должны различаться.')
        for row in rows:
            next_codes = {x.strip() for x in row.get('allowed_next', '').split(',') if x.strip()}
            if next_codes - codes:
                raise ValidationError('В следующих темах есть неизвестный код.')


class TopicInline(admin.StackedInline):
    model = Topic
    formset = TopicFormSet
    extra = 1
    min_num = 1
    max_num = 10


@admin.register(Scenario)
class ScenarioAdmin(AuditAdmin):
    list_display = ('title', 'enabled', 'time_limit_seconds', 'max_turns', 'test_link')
    list_filter = ('enabled',)
    search_fields = ('title',)
    inlines = [TopicInline]
    fieldsets = [('Диалоги · тест — отдельные задания', {'fields': ('title', 'enabled', 'situation', 'goal', 'persona'),
        'description': 'Изменения действуют только для новых прохождений. Hard-блоки ниже определяют допустимые темы.'}),
        ('Начало и ограничения', {'fields': ('first_message', 'first_topic', 'time_limit_seconds', 'max_turns')})]

    @admin.display(description='Тестирование')
    def test_link(self, obj):
        return format_html('<a href="{}">Открыть лабораторию →</a>', reverse('dialogues:index'))


@admin.register(DialoguePrompt)
class PromptAdmin(AuditAdmin):
    readonly_fields = ('version', 'updated_at')
    fields = ('version', 'client_text', 'evaluator_text', 'updated_at')

    def has_add_permission(self, request):
        return request.user.is_superuser and not DialoguePrompt.objects.exists()

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            previous = DialoguePrompt.objects.select_for_update().filter(pk=1).first()
            obj.pk = 1
            obj.version = previous.version+1 if previous else 1
            super().save_model(request, obj, form, change)


class ReadOnlyAdmin(admin.ModelAdmin):
    list_per_page = 10
    list_max_show_all = 0
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)


@admin.register(Session)
class SessionAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'user', 'scenario', 'status', 'score', 'open_chat')
    list_filter = ('status', 'scenario')

    @admin.display(description='Диалог')
    def open_chat(self, obj):
        return format_html('<a href="{}">Переписка и разбор →</a>', reverse('dialogues:session', args=[obj.pk]))


@admin.register(DialogueTrace)
class TraceAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'session', 'stage', 'model', 'duration_ms', 'error')
    list_filter = ('stage',)
