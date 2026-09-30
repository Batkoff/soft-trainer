"""Управление командой без выбора десятков разрешений Django."""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth.models import User, Group
from .people import ROLE_CHOICES, apply_role, user_role, display_name
from .models import UserProfile
from django.db.models import Q
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
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
        role = self.data.get("role") if self.is_bound else getattr(self, "selected_role", None) or user_role(self.instance)
        self.fields["role"].initial = role
        parent_role = {"employee": "group_leader", "group_leader": "sector_leader"}.get(role)
        child_role = {"group_leader": "employee", "sector_leader": "group_leader"}.get(role)
        self.fields["manager"].queryset = people.filter(profile__role=parent_role, is_active=True) if parent_role else people.none()
        if child_role == "employee":
            # Старые сотрудники могли быть созданы до появления UserProfile.
            # Считаем их сотрудниками и даём назначить РГ; при сохранении профиль создастся.
            self.fields["reports"].queryset = people.filter(Q(profile__role="employee") | Q(profile__isnull=True))
        else:
            self.fields["reports"].queryset = people.filter(profile__role=child_role) if child_role else people.none()
        self.fields["reports"].queryset = self.fields["reports"].queryset.filter(Q(profile__manager__isnull=True) | Q(profile__manager_id=self.instance.pk))
        profile = getattr(self.instance, "profile", None)
        self.fields["manager"].initial = profile.manager_id if profile and parent_role else None
        self.fields["reports"].initial = list(self.instance.direct_reports.values_list("user_id", flat=True)) if self.instance.pk and child_role else []
        self.fields["unit_name"].initial = profile.unit_name if profile else ""
        self.fields["unit_name"].label = "Название сектора" if role == "sector_leader" else "Название группы"
        self.fields["manager"].help_text = ""
        self.fields["reports"].help_text = "Доступны только свободные пользователи и уже назначенные вам. Для перевода сначала освободите пользователя в прежней группе."
        self.fields["reports"].label = "Сотрудники группы" if role == "group_leader" else "Руководители групп"
        # Не заменяем FilteredSelectMultiple: штатный виджет Django показывает
        # две колонки «доступные ↔ выбранные» и корректно отправляет выбранных.
        for name, visible in (("manager", parent_role), ("reports", child_role), ("unit_name", child_role)):
            if not visible:
                self.fields[name].widget = forms.MultipleHiddenInput() if name == "reports" else forms.HiddenInput()
        # Хэш не помогает администратору: оставляем штатную безопасную смену пароля.
        if "password" in self.fields:
            self.fields["password"].help_text = "Пароль нельзя посмотреть. Новый пароль можно задать отдельно."
            self.fields["password"].widget.template_name = "admin/auth/user/password_summary.html"

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
        # Блокировки живут до конца транзакции сохранения формы в Django admin.
        # Повторная проверка защищает от двух одновременных назначений.
        with transaction.atomic():
            selected = list(cleaned.get("reports", []))
            ids = sorted({p.pk for p in selected} | ({self.instance.pk} if self.instance.pk else set()))
            list(User.objects.select_for_update(of=("self",)).filter(pk__in=ids).order_by("pk"))
            current = UserProfile.objects.filter(user_id=self.instance.pk).first()
            if current and current.manager_id and manager and current.manager_id != manager.pk:
                self.add_error("manager", "Сначала освободите пользователя у текущего руководителя и сохраните карточку.")
            occupied = UserProfile.objects.filter(user_id__in=[p.pk for p in selected], manager__isnull=False).exclude(manager_id=self.instance.pk)
            if occupied.exists():
                self.add_error("reports", "Пользователь уже назначен другому руководителю. Сначала освободите его.")
        return cleaned

class TeamCreationForm(RoleFormMixin, UserCreationForm):
    unit_name = forms.CharField(label="Название группы", max_length=120, required=False)
    role = forms.ChoiceField(label="Роль", choices=ROLE_CHOICES)
    manager = PersonChoice(label="Руководитель", queryset=User.objects.none(), required=False,
        help_text="Сотрудник → РГ; руководитель группы → РС. Можно назначить позже.")
    reports = PeopleChoice(label="Подчинённые", queryset=User.objects.none(), required=False,
        widget=admin.widgets.FilteredSelectMultiple("Подчинённые", is_stacked=False),
        help_text="РГ: выберите сотрудников. РС: выберите РГ. Доступны только свободные пользователи и уже назначенные вам. Для перевода сначала освободите пользователя в прежней группе.")

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "manager", "reports")

class TeamChangeForm(RoleFormMixin, UserChangeForm):
    unit_name = forms.CharField(label="Название группы", max_length=120, required=False)
    role = forms.ChoiceField(label="Роль", choices=ROLE_CHOICES)
    manager = PersonChoice(label="Руководитель", queryset=User.objects.none(), required=False,
        help_text="Сотрудник → РГ; руководитель группы → РС. Можно назначить позже.")
    reports = PeopleChoice(label="Подчинённые", queryset=User.objects.none(), required=False,
        widget=admin.widgets.FilteredSelectMultiple("Подчинённые", is_stacked=False),
        help_text="РГ: выберите сотрудников. РС: выберите РГ. Доступны только свободные пользователи и уже назначенные вам. Для перевода сначала освободите пользователя в прежней группе.")


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
        ("Команда", {"fields": ("unit_name", "manager", "reports")}),
        ("История входов", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (("Новый сотрудник", {"fields": ("username", "first_name", "last_name", "email", "role", "unit_name", "manager", "reports", "password1", "password2")}),)
    filter_horizontal = ()
    actions = ("archive_users", "activate_users", "release_users")
    list_per_page = 10
    list_max_show_all = 0

    class Media:
        js = ("team-form.js",)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def get_form(self, request, obj=None, **kwargs):
        base = super().get_form(request, obj, **kwargs)
        class RoleForm(base):
            selected_role = request.GET.get("role") if request.GET.get("role") in dict(ROLE_CHOICES) else None
        return RoleForm

    def change_view(self, request, object_id, form_url="", extra_context=None):
        return super().change_view(request, object_id, form_url, {**(extra_context or {}), "title": "Изменить пользователя"})

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
        return request.user.is_superuser and (obj is None or (obj.pk != request.user.pk and not obj.is_superuser))

    @transaction.atomic
    def archive_accounts(self, request, queryset):
        people = list(queryset.select_for_update(of=("self",)).filter(is_superuser=False).exclude(pk=request.user.pk))
        for person in people:
            person.is_active = False
            person.save(update_fields=["is_active"])
            UserProfile.objects.filter(user=person).update(manager=None)
            UserProfile.objects.filter(manager=person).update(manager=None)
            audit(request.user, "user_archived", person, username=person.username)
        self.message_user(request, f"Доступ отключён: {len(people)}. Ответы и результаты сохранены.")

    def archive_confirmation(self, request, queryset, action=None):
        people = queryset.filter(is_superuser=False).exclude(pk=request.user.pk)
        if request.method == "POST" and request.POST.get("confirm_archive"):
            self.archive_accounts(request, people)
            return redirect("admin:auth_user_changelist")
        return TemplateResponse(request, "admin/auth/user/archive.html", {
            **self.admin_site.each_context(request), "title": "Отключить доступ к аккаунтам",
            "people": people, "action": action, "opts": self.model._meta,
        })

    def delete_view(self, request, object_id, extra_context=None):
        obj = self.get_object(request, object_id)
        if obj is None or not self.has_delete_permission(request, obj):
            raise PermissionDenied
        return redirect("team_delete", user_id=obj.pk)

    @admin.action(description="Отключить доступ (сохранить историю)")
    def archive_users(self, request, queryset):
        return self.archive_confirmation(request, queryset, "archive_users")

    @admin.action(description="Восстановить доступ выбранным")
    def activate_users(self, request, queryset):
        people = queryset.filter(is_superuser=False)
        with transaction.atomic():
            for person in people.select_for_update(of=("self",)):
                person.is_active = True
                person.save(update_fields=["is_active"])
                audit(request.user, "user_restored", person)
        self.message_user(request, "Доступ восстановлен. При необходимости назначьте руководителя.")

    @admin.action(description="Освободить от текущего руководителя")
    def release_users(self, request, queryset):
        with transaction.atomic():
            people = list(queryset.select_for_update(of=("self",)).filter(is_superuser=False).order_by("pk"))
            for person in people:
                UserProfile.objects.filter(user=person).update(manager=None)
                audit(request.user, "team_released", person)
        self.message_user(request, f"Освобождено пользователей: {len(people)}.")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        apply_role(obj, form.cleaned_data["role"])
        profile = UserProfile.objects.get(user=obj)
        previous_manager = profile.manager_id
        profile.manager = form.cleaned_data["manager"]
        profile.unit_name = form.cleaned_data["unit_name"] if form.cleaned_data["role"] in ("group_leader", "sector_leader") else ""
        profile.save(update_fields=["manager", "unit_name"])
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
