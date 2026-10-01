import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from .prompts import client_default, evaluator_default


class Scenario(models.Model):
    title = models.CharField('Название', max_length=160)
    situation = models.TextField('Ситуация и неизменные факты', max_length=6000)
    goal = models.TextField('Цель клиента', max_length=1500)
    persona = models.TextField('Характер и начальное настроение', max_length=1500)
    first_message = models.TextField('Первое сообщение клиента', max_length=1500)
    first_topic = models.SlugField('Код первой темы', default='start', max_length=40)
    time_limit_seconds = models.PositiveSmallIntegerField('Секунд на каждый ответ', default=180,
        validators=[MinValueValidator(30), MaxValueValidator(600)])
    max_turns = models.PositiveSmallIntegerField('Максимум ответов', default=5,
        validators=[MinValueValidator(2), MaxValueValidator(6)])
    enabled = models.BooleanField('Доступен для тестирования', default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-pk']
        verbose_name = 'сценарий диалога'
        verbose_name_plural = 'Сценарии диалогов'

    def __str__(self):
        return self.title


class Topic(models.Model):
    scenario = models.ForeignKey(Scenario, on_delete=models.CASCADE, related_name='topics')
    code = models.SlugField('Код темы', max_length=40)
    title = models.CharField('Тема вопроса', max_length=120)
    hard_answer = models.TextField('Правильный hard-ответ', max_length=4000)
    required_facts = models.TextField('Обязательные факты', max_length=3000,
        help_text='По одному на строку. При оценке учитывается уже сказанное в диалоге.')
    allowed_next = models.CharField('Следующие темы', blank=True, max_length=400,
        help_text='Коды через запятую. Пусто — любая тема сценария. Текущую тему можно уточнять всегда.')

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['scenario', 'code'], name='dialogue_topic_code')]
        verbose_name = 'hard-блок диалога'
        verbose_name_plural = 'Hard-блоки диалога'

    def __str__(self):
        return f'{self.code}: {self.title}'


class DialoguePrompt(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    client_text = models.TextField('Промпт клиента', default=client_default, max_length=16000)
    evaluator_text = models.TextField('Промпт оценщика диалогов', default=evaluator_default, max_length=20000)
    version = models.PositiveIntegerField('Версия', default=1, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'промпты диалогов'
        verbose_name_plural = 'Промпты диалогов'

    def clean(self):
        if not self.client_text.strip() or not self.evaluator_text.strip():
            raise ValidationError("Оба промпта должны быть заполнены.")

    def __str__(self):
        return f'Клиент и оценщик · версия {self.version}'


class Session(models.Model):
    class Status(models.TextChoices):
        READY = 'ready', 'Готов следующий ход'
        WRITING = 'writing', 'Ожидает ответа'
        GENERATING = 'generating', 'Клиент печатает'
        EVALUATING = 'evaluating', 'Идёт итоговая оценка'
        COMPLETED = 'completed', 'Завершён'
        FAILED = 'failed', 'Нужен повтор запроса'
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    scenario = models.ForeignKey(Scenario, on_delete=models.PROTECT)
    snapshot = models.JSONField(default=dict)
    prompts = models.JSONField(default=dict)
    profile = models.JSONField(default=dict)
    status = models.CharField('Статус', choices=Status.choices, default=Status.READY, max_length=16)
    auto_advance = models.BooleanField(default=True)
    closing_message = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    score = models.PositiveSmallIntegerField('Баллы', null=True)
    operation = models.UUIDField(default=uuid.uuid4)
    processing_token = models.UUIDField(null=True)
    claimed_at = models.DateTimeField(null=True)
    job_tries = models.PositiveSmallIntegerField(default=0)
    failed_stage = models.CharField(max_length=16, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'прохождение диалога'
        verbose_name_plural = 'История диалогов'

    def __str__(self):
        return f'{self.snapshot.get("title", "Диалог")} · {self.get_status_display()}'


class Turn(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='turns')
    number = models.PositiveSmallIntegerField()
    topic_code = models.CharField(max_length=40)
    client_message = models.TextField()
    emotion = models.CharField(max_length=80, blank=True)
    answer = models.TextField(blank=True, max_length=6000)
    revision = models.PositiveIntegerField(default=0)
    opened_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)
    submitted_at = models.DateTimeField(null=True)
    timed_out = models.BooleanField(default=False)

    class Meta:
        ordering = ['number']
        constraints = [models.UniqueConstraint(fields=['session', 'number'], name='dialogue_turn_number')]


class DialogueTrace(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='traces')
    stage = models.CharField('Этап', max_length=16, choices=[('client', 'Клиент'), ('evaluation', 'Оценщик')])
    provider = models.CharField(max_length=40)
    model = models.CharField(max_length=200)
    prompt_version = models.PositiveIntegerField()
    request_payload = models.JSONField(default=dict)
    response_payload = models.JSONField(default=dict)
    normalized_payload = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    usage = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-pk']
        verbose_name = 'запрос диалога'
        verbose_name_plural = 'Запросы диалогов к нейросети'
