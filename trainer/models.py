"""Храним попытки и версии явно: оценку всегда можно объяснить и проверить."""
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

SKILLS = {"clarity": "Ясность", "tone": "Тон общения", "empathy": "Эмпатия",
          "expectations": "Управление ожиданиями", "initiative": "Инициативность"}

class UserProfile(models.Model):
    """Аватар из набора не требует хранения файлов и отдельного сервиса картинок."""
    AVATARS = [("🙂", "🙂 Улыбка"), ("🦊", "🦊 Лиса"), ("🐱", "🐱 Кот"),
               ("🐼", "🐼 Панда"), ("🚀", "🚀 Ракета"), ("🌿", "🌿 Лист")]
    class Role(models.TextChoices):
        EMPLOYEE = "employee", "Сотрудник"
        GROUP_LEADER = "group_leader", "Руководитель группы"
        SECTOR_LEADER = "sector_leader", "Руководитель сектора"

    role = models.CharField("Роль", max_length=20, choices=Role.choices, default=Role.EMPLOYEE)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="direct_reports", verbose_name="Руководитель")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    unit_name = models.CharField("Название группы или сектора", max_length=120, blank=True)
    display_name = models.CharField("Отображаемый ник", max_length=60, blank=True)
    avatar = models.CharField("Аватар", max_length=8, choices=AVATARS, default="🙂")

def default_rubric():
    from .evaluation_profiles import current_profile
    return {"version": "soft-v1", "weights": {key: 1 for key in SKILLS}, "evaluator": current_profile()}

def demo_rubric():
    return {"version": "demo-v1", "weights": {key: 1 for key in SKILLS}}

class Exercise(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PUBLISHED = "published", "Опубликовано"
        ARCHIVED = "archived", "В архиве"
    title = models.CharField("Название", max_length=160)
    category = models.CharField("Тема", max_length=80, default="Поддержка бизнеса")
    customer_message = models.TextField("Сообщение клиента", max_length=4000)
    hard_answer = models.TextField("Исходный hard-ответ", max_length=4000)
    required_facts = models.TextField("Обязательные факты", help_text="Один факт на строку.")
    allowed_actions = models.TextField("Допустимые следующие действия", blank=True)
    forbidden_promises = models.TextField("Запрещённые обещания", blank=True)
    status = models.CharField("Статус", max_length=12, choices=Status.choices, default=Status.DRAFT)
    version = models.PositiveIntegerField("Версия", default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "задание"
        verbose_name_plural = "Задания"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} · v{self.version}"

    def clean(self):
        if self.pk:
            previous = Exercise.objects.get(pk=self.pk)
            frozen = ("title", "category", "customer_message", "hard_answer", "required_facts",
                      "allowed_actions", "forbidden_promises", "version")
            if previous.status != self.Status.DRAFT:
                if any(getattr(self, key) != getattr(previous, key) for key in frozen):
                    raise ValidationError("Опубликованное задание неизменно. Создайте новую версию через действие в списке.")
                if self.status == self.Status.DRAFT:
                    raise ValidationError("Для изменений создайте новую версию задания.")

    def snapshot(self):
        return {key: getattr(self, key) for key in (
            "id", "title", "category", "version", "customer_message", "hard_answer",
            "required_facts", "allowed_actions", "forbidden_promises")}

class Contest(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Идёт"
        FINISHED = "finished", "Итоги зафиксированы"
    title = models.CharField("Название", max_length=160)
    starts_at = models.DateTimeField("Начало")
    ends_at = models.DateTimeField("Окончание")
    daily_limit = models.PositiveSmallIntegerField("Заданий в день", default=5,
        validators=[MinValueValidator(1), MaxValueValidator(10)])
    time_limit_seconds = models.PositiveSmallIntegerField("Секунд на задание", default=180,
        validators=[MinValueValidator(30), MaxValueValidator(1800)])
    status = models.CharField("Статус", max_length=12, choices=Status.choices, default=Status.DRAFT)
    participants = models.ManyToManyField(settings.AUTH_USER_MODEL, verbose_name="Участники", blank=True)
    exercises = models.ManyToManyField(Exercise, verbose_name="Задания", blank=True)
    rubric = models.JSONField("Профиль оценки", default=default_rubric)
    first_prize = models.PositiveIntegerField("1 место, T-Money", default=100)
    second_prize = models.PositiveIntegerField("2 место, T-Money", default=70)
    third_prize = models.PositiveIntegerField("3 место, T-Money", default=50)
    final_standings = models.JSONField("Зафиксированный рейтинг", default=list, editable=False)
    finalized_at = models.DateTimeField(null=True, editable=False)
    closed_at = models.DateTimeField("Приём завершён досрочно", null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "конкурс"
        verbose_name_plural = "Конкурсы"
        ordering = ["-starts_at"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError("Окончание должно быть позже начала.")
        if self.pk:
            previous = Contest.objects.get(pk=self.pk)
            if previous.status != self.Status.DRAFT:
                fields = ("starts_at", "ends_at", "daily_limit", "time_limit_seconds", "rubric",
                          "first_prize", "second_prize", "third_prize")
                if any(getattr(self, key) != getattr(previous, key) for key in fields):
                    raise ValidationError("Правила запущенного конкурса зафиксированы. Создайте новый конкурс.")

    @property
    def accepts_answers(self):
        return self.status == self.Status.ACTIVE and self.starts_at <= timezone.now() < self.effective_end

    @property
    def effective_end(self):
        """Плановые сроки сохраняем для истории, досрочное закрытие — отдельно."""
        return min(self.ends_at, self.closed_at) if self.closed_at else self.ends_at

    @property
    def phase(self):
        if self.status == self.Status.FINISHED:
            return "finished"
        if self.status == self.Status.DRAFT:
            return "draft"
        now = timezone.now()
        if self.effective_end <= now:
            return "closing"
        return "scheduled" if self.starts_at > now else "running"

    @property
    def phase_label(self):
        return {"finished": "Итоги", "draft": "Черновик", "closing": "Подводим итоги",
                "scheduled": "Скоро", "running": "Идёт сейчас"}[self.phase]

    @property
    def availability_message(self):
        if self.phase == "scheduled":
            start = timezone.localtime(self.starts_at).strftime("%d.%m.%Y в %H:%M:%S")
            return f"Конкурс начнётся {start} МСК. Задания откроются автоматически."
        return {"finished": "Конкурс завершён. Итоговый рейтинг сохранён в архиве.",
                "closing": "Приём ответов завершён. Ожидаем проверки и пересмотра оставшихся работ.",
                "draft": "Конкурс ещё не запущен администратором.", "running": "Конкурс идёт."}[self.phase]

class Assignment(models.Model):
    """Уникальный слот исключает шестую попытку и повторы из других вкладок."""
    contest = models.ForeignKey(Contest, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    day = models.DateField()
    slot = models.PositiveSmallIntegerField()
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["contest", "user", "day", "slot"], name="unique_daily_slot")]
        ordering = ["day", "slot"]

class Attempt(models.Model):
    class Status(models.TextChoices):
        WRITING = "writing", "В работе"
        QUEUED = "queued", "В очереди"
        EVALUATING = "evaluating", "Проверяется"
        RETRY = "retry", "Повторная проверка"
        GRADED = "graded", "Проверено"
        REVIEW = "review", "Нужен пересмотр"
    class Mode(models.TextChoices):
        RATED = "rated", "Конкурс"
        SANDBOX = "sandbox", "Песочница"
    class Scenario(models.TextChoices):
        REAL = "real", "Реальная оценка нейросетью"
        NORMAL = "normal", "Успешная тестовая оценка"
        HARD_ERROR = "hard_error", "Тест: ошибка в hard-части"
        TRANSIENT = "transient", "Тест: временный сбой и повтор"
        FAILURE = "failure", "Тест: постоянный сбой"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="Сотрудник")
    contest = models.ForeignKey(Contest, on_delete=models.PROTECT, null=True, blank=True, verbose_name="Конкурс")
    assignment = models.OneToOneField(Assignment, on_delete=models.PROTECT, null=True, blank=True)
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT, verbose_name="Задание")
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.RATED)
    snapshot = models.JSONField(default=dict)
    rubric = models.JSONField(default=default_rubric)
    status = models.CharField("Статус", max_length=12, choices=Status.choices, default=Status.WRITING)
    answer = models.TextField("Ответ", max_length=6000, blank=True)
    draft_revision = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True)
    graded_at = models.DateTimeField(null=True)
    timed_out = models.BooleanField("Время вышло", default=False)
    queue_job_id = models.BigIntegerField(null=True)
    evaluation_tries = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField("Ошибка", max_length=240, blank=True)
    score = models.PositiveSmallIntegerField("Баллы", null=True, validators=[MaxValueValidator(100)])
    reviewed_skills = models.JSONField("Навыки после пересмотра", default=dict, editable=False)
    hard_verdict = models.CharField("Hard", max_length=12, default="unverified",
        choices=[("unverified", "Не проверялся"), ("passed", "Сохранён"), ("violated", "Искажён"),
                 ("uncertain", "Нужна ручная проверка")])
    demo_scenario = models.CharField(max_length=12, choices=Scenario.choices, default=Scenario.NORMAL)

    class Meta:
        verbose_name = "попытка"
        verbose_name_plural = "Попытки"
        permissions = [("review_attempt", "Пересматривать оценки сотрудников")]
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["contest", "mode", "status"]), models.Index(fields=["status", "expires_at"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(score__isnull=True) | models.Q(score__lte=100), name="score_range"),
            models.CheckConstraint(condition=(models.Q(mode="rated", contest__isnull=False, assignment__isnull=False)
                | models.Q(mode="sandbox", contest__isnull=True, assignment__isnull=True)), name="attempt_mode_links"),
        ]

    def __str__(self):
        return f"{self.user} · {self.snapshot.get('title', '')}"

class Evaluation(models.Model):
    class Meta:
        verbose_name = "результат проверки"
        verbose_name_plural = "Результаты проверки"

    """Первоначальный ответ обработчика не переписывается ручным пересмотром."""
    attempt = models.OneToOneField(Attempt, on_delete=models.PROTECT, related_name="evaluation")
    payload = models.JSONField()
    backend = models.CharField(max_length=40, default="demo")
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class EvaluationTrace(models.Model):
    """Полный технический след одной попытки проверки.

    Тело запроса содержит системный промпт и текст задания, а тело ответа —
    ответ провайдера. Эти данные нужны для отладки и доступны только через
    staff-admin; ключ API сюда никогда не попадает.
    """
    class Status(models.TextChoices):
        SUCCESS = "success", "Успешно"
        RETRY = "retry", "Сбой · назначен повтор"
        FAILED = "failed", "Ошибка"
        REVIEW = "review", "На пересмотре"

    attempt = models.ForeignKey(Attempt, on_delete=models.PROTECT, related_name="evaluation_traces",
                                verbose_name="Попытка")
    try_number = models.PositiveSmallIntegerField("Попытка API", default=1)
    provider = models.CharField("Провайдер", max_length=40, blank=True)
    model = models.CharField("Модель", max_length=160, blank=True)
    prompt_version = models.CharField("Версия промпта", max_length=40, blank=True)
    request_payload = models.JSONField("Запрос (промпт)", default=dict)
    response_payload = models.JSONField("Ответ провайдера", default=dict)
    normalized_payload = models.JSONField("Нормализованный разбор", default=dict)
    status = models.CharField("Статус", max_length=12, choices=Status.choices)
    error_type = models.CharField("Тип ошибки", max_length=80, blank=True)
    error_message = models.CharField("Безопасное сообщение", max_length=240, blank=True)
    error_metadata = models.JSONField("Технические детали ошибки", default=dict)
    duration_ms = models.PositiveIntegerField("Время, мс", default=0)
    input_tokens = models.PositiveIntegerField("Входные токены", null=True, blank=True)
    output_tokens = models.PositiveIntegerField("Выходные токены", null=True, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "диагностика оценщика"
        verbose_name_plural = "Диагностика оценщика"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["attempt", "created_at"]),
                   models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.attempt_id} · попытка API {self.try_number} · {self.get_status_display()}"

class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField("Событие", max_length=64)
    object_id = models.CharField("Объект", max_length=64)
    details = models.JSONField("Данные", default=dict)
    created_at = models.DateTimeField("Время", auto_now_add=True)

    class Meta:
        verbose_name = "событие"
        verbose_name_plural = "Журнал действий"
        ordering = ["-created_at"]

    def __str__(self):
        from .diagnostics import event_title
        return f"{event_title(self)} · #{self.pk}"

class CalibrationCase(models.Model):
    title = models.CharField("Название", max_length=160)
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT, verbose_name="Задание")
    answer = models.TextField("Пример ответа", max_length=6000)
    expected_min = models.PositiveSmallIntegerField("Ожидаемый минимум", default=75, validators=[MaxValueValidator(100)])
    expected_max = models.PositiveSmallIntegerField("Ожидаемый максимум", default=85, validators=[MaxValueValidator(100)])
    expected_hard = models.CharField("Ожидаемый hard", max_length=12, default="unverified",
        choices=Attempt._meta.get_field("hard_verdict").choices)
    scenario = models.CharField("Тестовый сценарий", max_length=12, choices=Attempt.Scenario.choices, default="normal")

    class Meta:
        verbose_name = "пример калибровки"
        verbose_name_plural = "Эталонные ответы"

    def __str__(self):
        return self.title

    def clean(self):
        if self.expected_min > self.expected_max:
            raise ValidationError("Минимум не может быть больше максимума.")

class CalibrationRun(models.Model):
    class Meta:
        verbose_name = "проверка эталона"
        verbose_name_plural = "Проверки эталонов"

    case = models.ForeignKey(CalibrationCase, on_delete=models.PROTECT)
    attempt = models.OneToOneField(Attempt, on_delete=models.PROTECT)
    expected_min = models.PositiveSmallIntegerField()
    expected_max = models.PositiveSmallIntegerField()
    expected_hard = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)

class LoginThrottle(models.Model):
    """Общий лимит входов работает между несколькими процессами веб-сервера."""
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField(default=timezone.now)


class AIConfiguration(models.Model):
    """Единственная настройка подключения; ключи зашифрованы SECRET_KEY проекта."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    provider = models.CharField(max_length=20, default="openrouter")
    model = models.CharField(max_length=200)
    openai_secret = models.TextField(blank=True)
    openrouter_secret = models.TextField(blank=True)
    proxy_enabled = models.BooleanField("Использовать прокси", default=False)
    proxy_url = models.CharField("Адрес HTTP(S)-прокси", max_length=500, blank=True)
    proxy_username = models.CharField("Логин прокси", max_length=255, blank=True)
    proxy_secret = models.TextField("Пароль прокси", blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class EvaluationPrompt(models.Model):
    """Одна текущая настройка; в профиле конкурса хранится неизменяемая копия."""
    from .evaluation_prompt import default_evaluation_prompt
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    version = models.PositiveSmallIntegerField("Версия", default=3, editable=False)
    text = models.TextField("Текст промпта", default=default_evaluation_prompt, max_length=20000)
    updated_at = models.DateTimeField("Изменён", auto_now=True)

    class Meta:
        verbose_name = "Промпт оценки"
        verbose_name_plural = "Промпт оценки"

    def __str__(self):
        return "Правила оценки новых конкурсов и песочницы"
