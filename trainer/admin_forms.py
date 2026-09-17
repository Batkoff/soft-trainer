"""Поля сроков конкурса: местное время, секунды, без долей секунды."""
from django import forms
from django.contrib.admin.widgets import AdminSplitDateTime
from .models import Contest


class ContestDateTimeWidget(AdminSplitDateTime):
    def __init__(self, attrs=None):
        super().__init__(attrs=attrs)
        self.widgets[1].format = "%H:%M:%S"
        self.widgets[1].attrs.update(placeholder="00:00:00")

    def decompress(self, value):
        date, time = super().decompress(value)
        return [date, time if time is not None else "00:00:00"]


class ContestDateTimeField(forms.SplitDateTimeField):
    def __init__(self, **kwargs):
        super().__init__(widget=ContestDateTimeWidget(), input_time_formats=["%H:%M:%S", "%H:%M"],
                         help_text="Московское время. Если время не указано — 00:00:00.", **kwargs)

    def clean(self, value):
        if isinstance(value, (list, tuple)) and len(value) == 2 and value[0] and not value[1]:
            value = [value[0], "00:00:00"]
        return super().clean(value)


class ContestAdminForm(forms.ModelForm):
    starts_at = ContestDateTimeField(label="Начало")
    ends_at = ContestDateTimeField(label="Окончание")

    class Meta:
        model = Contest
        fields = "__all__"
