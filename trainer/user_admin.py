"""Управление командой без выбора десятков разрешений Django."""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth.models import User, Group
from .people import ROLE_CHOICES, apply_role, user_role
from .services import audit

class RoleFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].initial = user_role(self.instance)

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.is_active and self.instance.is_superuser:
            removes_admin = cleaned.get("role") != "admin" or not cleaned.get("is_active", True)
            if removes_admin and not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=self.instance.pk).exists():
                raise forms.ValidationError("Нельзя отключить последнего администратора. Сначала назначьте другого.")
        return cleaned

class TeamCreationForm(RoleFormMixin, UserCreationForm):
    role = forms.ChoiceField(label="Роль", choices=ROLE_CHOICES)
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "role")

class TeamChangeForm(RoleFormMixin, UserChangeForm):
    role = forms.ChoiceField(label="Роль", choices=ROLE_CHOICES)

admin.site.unregister(User)
admin.site.unregister(Group)  # Роли задаются в карточке сотрудника.

@admin.register(User)
class TeamAdmin(UserAdmin):
    form = TeamChangeForm
    add_form = TeamCreationForm
    list_display = ("username", "first_name", "last_name", "role_name", "is_active", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser")
    readonly_fields = ("last_login", "date_joined")
    fieldsets = (
        ("Сотрудник", {"fields": ("username", "first_name", "last_name", "email")}),
        ("Доступ", {"fields": ("role", "is_active", "password"),
                    "description": "Руководитель видит работы и аналитику пилота, пересматривает оценки. Только администратор управляет пользователями, заданиями и конкурсами."}),
        ("История входов", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (("Новый сотрудник", {"fields": ("username", "first_name", "last_name", "email", "role", "password1", "password2")}),)
    filter_horizontal = ()
    actions = None

    @admin.display(description="Роль")
    def role_name(self, obj):
        return dict(ROLE_CHOICES)[user_role(obj)]

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        apply_role(obj, form.cleaned_data["role"])
        audit(request.user, "user_updated" if change else "user_created", obj,
              role=form.cleaned_data["role"], active=obj.is_active)
