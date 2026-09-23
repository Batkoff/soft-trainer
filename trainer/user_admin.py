"""Управление командой без выбора десятков разрешений Django."""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth.models import User, Group
from .people import ROLE_CHOICES, apply_role, user_role, display_name
from .models import UserProfile
from django.db.models import Q
from .services import audit

class PersonChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{display_name(obj)} · {obj.username}"


class PeopleChoice(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return f"{display_name(obj)} · {obj.username}"


class RoleFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].initial = user_role(self.instance)
        people = User.objects.filter(is_superuser=False).exclude(pk=self.instance.pk).select_related("profile")
        self.fields["manager"].queryset = people.filter(profile__role__in=["group_leader", "sector_leader"], is_active=True)
        self.fields["reports"].queryset = people.exclude(profile__role="sector_leader")
        profile = getattr(self.instance, "profile", None)
        self.fields["manager"].initial = profile.manager_id if profile else None
        self.fields["reports"].initial = list(self.instance.direct_reports.values_list("user_id", flat=True)) if self.instance.pk else []

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and self.instance.is_active and self.instance.is_superuser:
            removes_admin = cleaned.get("role") != "admin" or not cleaned.get("is_active", True)
            if removes_admin and not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=self.instance.pk).exists():
                raise forms.ValidationError("Нельзя отключить последнего администратора. Сначала назначьте другого.")
        role = cleaned.get("role")
        manager = cleaned.get("manager")
        expected = {"employee": "group_leader", "group_leader": "sector_leader"}.get(role)
        if manager and (manager.pk == self.instance.pk or user_role(manager) != expected):
            self.add_error("manager", "Для сотрудника выберите РГ, для РГ — РС. У РС и администратора нет руководителя.")
        child_role = {"group_leader": "employee", "sector_leader": "group_leader"}.get(role)
        for person in cleaned.get("reports", []):
            if not child_role or user_role(person) != child_role:
                self.add_error("reports", "РГ может включать сотрудников; РС — руководителей групп.")
                break
        if self.instance.pk and role != user_role(self.instance) and self.instance.direct_reports.exists():
            self.add_error("role", "Сначала переведите подчинённых к другому руководителю.")
        return cleaned

class TeamCreationForm(RoleFormMixin, UserCreationForm):
    role = forms.ChoiceField(label="Роль", choices=ROLE_CHOICES)
    manager = PersonChoice(label="Руководитель", queryset=User.objects.none(), required=False,
        help_text="Сотрудник → РГ; руководитель группы → РС. Можно назначить позже.")
    reports = PeopleChoice(label="Подчинённые", queryset=User.objects.none(), required=False,
        widget=admin.widgets.FilteredSelectMultiple("Подчинённые", is_stacked=False),
        help_text="РГ: выберите сотрудников. РС: выберите РГ. Перенос из другой команды выполняется при сохранении.")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "manager", "reports")

class TeamChangeForm(RoleFormMixin, UserChangeForm):
    role = forms.ChoiceField(label="Роль", choices=ROLE_CHOICES)
    manager = PersonChoice(label="Руководитель", queryset=User.objects.none(), required=False,
        help_text="Сотрудник → РГ; руководитель группы → РС. Можно назначить позже.")
    reports = PeopleChoice(label="Подчинённые", queryset=User.objects.none(), required=False,
        widget=admin.widgets.FilteredSelectMultiple("Подчинённые", is_stacked=False),
        help_text="РГ: выберите сотрудников. РС: выберите РГ. Перенос из другой команды выполняется при сохранении.")


admin.site.unregister(User)
admin.site.unregister(Group)  # Роли задаются в карточке сотрудника.

@admin.register(User)
class TeamAdmin(UserAdmin):
    form = TeamChangeForm
    add_form = TeamCreationForm
    list_display = ("username", "first_name", "last_name", "role_name", "manager_name", "is_active", "last_login")
    list_filter = ("is_active", "profile__role", "is_superuser")
    readonly_fields = ("last_login", "date_joined")
    fieldsets = (
        ("Сотрудник", {"fields": ("username", "first_name", "last_name", "email")}),
        ("Доступ", {"fields": ("role", "is_active", "password"),
                    "description": "РГ видит свою группу, РС — свои группы. Только администратор имеет доступ к админке."}),
        ("Команда", {"fields": ("manager", "reports")}),
        ("История входов", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (("Новый сотрудник", {"fields": ("username", "first_name", "last_name", "email", "role", "manager", "reports", "password1", "password2")}),)
    filter_horizontal = ()
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("profile__manager")

    @admin.display(description="Руководитель")
    def manager_name(self, obj):
        profile = getattr(obj, "profile", None)
        return display_name(profile.manager) if profile and profile.manager_id else "—"

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
        profile = UserProfile.objects.get(user=obj)
        previous_manager = profile.manager_id
        profile.manager = form.cleaned_data["manager"]
        profile.save(update_fields=["manager"])
        reports = list(form.cleaned_data["reports"])
        report_ids = [person.pk for person in reports]
        removed = list(UserProfile.objects.filter(manager=obj).exclude(user_id__in=report_ids).values_list("user_id", flat=True))
        UserProfile.objects.filter(manager=obj).exclude(user_id__in=report_ids).update(manager=None)
        for person in reports:
            child, _ = UserProfile.objects.get_or_create(user=person)
            old_manager = child.manager_id
            child.manager = obj
            child.save(update_fields=["manager"])
            if old_manager != obj.pk:
                audit(request.user, "team_assignment", person, previous_manager=old_manager, manager=obj.pk)
        audit(request.user, "team_updated", obj, previous_manager=previous_manager,
              manager=profile.manager_id, reports=report_ids, removed=removed)
        audit(request.user, "user_updated" if change else "user_created", obj,
              role=form.cleaned_data["role"], active=obj.is_active)
