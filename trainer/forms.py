from django import forms
from django.conf import settings
from .models import Attempt, Exercise, SKILLS, UserProfile

class SandboxForm(forms.Form):
    exercise = forms.ModelChoiceField(label="Задание", queryset=Exercise.objects.all())
    scenario = forms.ChoiceField(label="Сценарий проверки", choices=Attempt.Scenario.choices)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scenario"].initial = "real" if settings.EVALUATOR_BACKEND != "demo" else "normal"

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
