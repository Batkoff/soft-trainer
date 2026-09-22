from django import forms
from django.conf import settings
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
    class Meta:
        model = UserProfile
        fields = ["display_name", "avatar"]
        widgets = {"avatar": forms.RadioSelect}

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
        return values
