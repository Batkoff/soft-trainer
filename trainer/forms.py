from django import forms
from django.conf import settings
from urllib.parse import urlsplit
from .models import Attempt, Exercise, SKILLS, UserProfile

class SandboxForm(forms.Form):
    exercise = forms.ModelChoiceField(label="Задание", queryset=Exercise.objects.all())
    scenario = forms.ChoiceField(label="Сценарий проверки", choices=Attempt.Scenario.choices)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scenario"].initial = "real"
        if not getattr(settings, "ALLOW_TEST_EVALUATOR", False):
            self.fields["scenario"].choices = [("real", "Оценка нейросетью")]
            self.fields["scenario"].widget = forms.HiddenInput()

class ReviewForm(forms.Form):
    hard_verdict = forms.ChoiceField(label="Hard-информация", choices=[("passed", "Сохранена"), ("violated", "Существенно искажена")])
    reason = forms.CharField(label="Причина пересмотра", max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, soft_scores=None, **kwargs):
        super().__init__(*args, **kwargs)
        for key, label in SKILLS.items():
            self.fields[f"soft_{key}"] = forms.IntegerField(label=f"{label} · 0–100", min_value=0, max_value=100,
                initial=(soft_scores or {}).get(key))
        self.order_fields(["hard_verdict", *(f"soft_{key}" for key in SKILLS), "reason"])

    def clean(self):
        cleaned = super().clean()
        if all(f"soft_{key}" in cleaned for key in SKILLS):
            cleaned["soft_scores"] = {key: cleaned.pop(f"soft_{key}") for key in SKILLS}
        return cleaned

class ProfileForm(forms.ModelForm):
    photo = forms.FileField(label="Фото", required=False,
        widget=forms.FileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
        help_text="JPG, PNG или WebP, до 5 МБ. Фото будет обрезано до квадрата.")
    remove_photo = forms.BooleanField(label="Удалить фото", required=False)

    class Meta:
        model = UserProfile
        fields = ["display_name"]

    def clean_photo(self):
        upload = self.cleaned_data.get("photo")
        if not upload:
            return None
        from .avatars import normalize_photo
        return normalize_photo(upload)

    def clean(self):
        data = super().clean()
        if data.get("photo") and data.get("remove_photo"):
            self.add_error("remove_photo", "Выберите загрузку нового фото или удаление текущего.")
        return data

    def save(self, commit=True):
        from hashlib import sha256
        profile = super().save(commit=False)
        photo = self.cleaned_data.get("photo")
        if photo is not None or self.cleaned_data.get("remove_photo"):
            profile.avatar_data = photo or b""
            profile.avatar_version = sha256(photo).hexdigest() if photo else ""
        if commit:
            profile.save()
        return profile

class AIConfigurationForm(forms.Form):
    provider = forms.ChoiceField(label="Провайдер", choices=[("openrouter", "OpenRouter"), ("openai", "OpenAI")])
    model_choice = forms.ChoiceField(label="Модель", choices=[
        ("nvidia/nemotron-3-super-120b-a12b:free", "OpenRouter · Nemotron Super · бесплатный вариант"),
        ("openai/gpt-4.1-mini", "OpenRouter · GPT-4.1 mini"),
        ("gpt-4.1-mini-2025-04-14", "OpenAI · GPT-4.1 mini"),
        ("custom", "Другая модель — указать идентификатор")])
    custom_model = forms.CharField(label="Идентификатор другой модели", max_length=200, required=False)
    api_key = forms.CharField(label="Новый API-ключ", max_length=1000, required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Оставьте пустым, чтобы сохранить ключ выбранного провайдера.")
    proxy_enabled = forms.BooleanField(label="Использовать прокси для запросов к нейросети", required=False)
    proxy_url = forms.CharField(label="HTTP(S)-прокси", max_length=500, required=False,
        widget=forms.URLInput(attrs={"placeholder": "http://proxy.example:3128"}),
        help_text="CONNECT-прокси. Укажите только схему, хост и порт; логин и пароль — ниже.")
    proxy_username = forms.CharField(label="Логин прокси", max_length=255, required=False)
    proxy_password = forms.CharField(label="Новый пароль прокси", max_length=1000, required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Оставьте пустым, чтобы сохранить ранее введённый пароль.")

    def clean(self):
        values = super().clean()
        model = values.get("model_choice")
        if model == "custom":
            model = values.get("custom_model", "").strip()
        if not model or any(c.isspace() for c in model):
            self.add_error("custom_model", "Укажите идентификатор без пробелов из кабинета провайдера.")
        elif values.get("model_choice") != "custom" and ((values.get("provider") == "openai") != model.startswith("gpt-")):
            self.add_error("model_choice", "Выберите модель для указанного провайдера.")
        values["model"] = model
        if any(c.isspace() for c in values.get("api_key", "")):
            self.add_error("api_key", "Ключ не должен содержать пробелы или переносы строк.")

        proxy_url = (values.get("proxy_url") or "").strip()
        if values.get("proxy_enabled") and not proxy_url:
            self.add_error("proxy_url", "Укажите адрес прокси.")
        if proxy_url:
            parsed = urlsplit(proxy_url)
            try:
                port = parsed.port
            except ValueError:
                port = None
            if parsed.scheme not in ("http", "https") or not parsed.hostname or not port:
                self.add_error("proxy_url", "Формат: http://host:port или https://host:port.")
            elif parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                self.add_error("proxy_url", "В адресе оставьте только схему, хост и порт. Авторизация вводится отдельно.")
        if values.get("proxy_password") and not (values.get("proxy_username") or "").strip():
            self.add_error("proxy_username", "Для пароля прокси укажите логин.")
        if "\n" in values.get("proxy_username", "") or "\r" in values.get("proxy_username", ""):
            self.add_error("proxy_username", "Логин прокси не должен содержать переносы строк.")
        return values
