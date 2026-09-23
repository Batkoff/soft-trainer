"""Область доступа определяется текущей иерархией, а не флагом is_staff."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

ROLE_CHOICES = [("employee", "Сотрудник"), ("group_leader", "Руководитель группы"),
                ("sector_leader", "Руководитель сектора"), ("admin", "Администратор")]
LEADER_GROUP = "Руководители тренажёра"


def user_role(user):
    if user.is_superuser:
        return "admin"
    # Запрашиваем актуальную роль: уже загруженный профиль мог устареть после перевода.
    from .models import UserProfile
    return UserProfile.objects.filter(user_id=user.pk).values_list("role", flat=True).first() or "employee"


def is_manager(user):
    return user.is_active and user_role(user) in ("admin", "sector_leader", "group_leader")


def managed_users(user):
    """Без собственного аккаунта. РС видит РГ и сотрудников этих РГ."""
    User = get_user_model()
    if not user.is_active:
        return User.objects.none()
    role = user_role(user)
    if role == "admin":
        return User.objects.all()
    direct = Q(profile__manager_id=user.pk, profile__role="employee")
    if role == "sector_leader":
        direct = Q(profile__manager_id=user.pk, profile__role="group_leader") | Q(
            profile__role="employee", profile__manager__profile__role="group_leader",
            profile__manager__profile__manager_id=user.pk)
    elif role != "group_leader":
        return User.objects.none()
    return User.objects.filter(direct, is_superuser=False).exclude(pk=user.pk)


def visible_users(user):
    return get_user_model().objects.filter(Q(pk=user.pk) | Q(pk__in=managed_users(user)))


def can_review(user, attempt=None):
    if not is_manager(user):
        return False
    if attempt is None or user.is_superuser:
        return True
    return managed_users(user).filter(pk=attempt.user_id).exists()


def display_name(user):
    profile = getattr(user, "profile", None)
    return (profile.display_name if profile else "") or user.get_full_name() or user.username


@transaction.atomic
def apply_role(user, role):
    """Единая роль заменяет разрозненные разрешения Django."""
    from .models import UserProfile
    role = "group_leader" if role == "leader" else role  # совместимость старых вызовов
    if role not in dict(ROLE_CHOICES):
        raise ValidationError("Неизвестная роль")
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.role != role and user.direct_reports.exists():
        raise ValidationError("Сначала переведите подчинённых к другому руководителю.")
    user.is_staff = role != "employee"
    user.is_superuser = role == "admin"
    user.save(update_fields=["is_staff", "is_superuser"])
    profile.role = "employee" if role == "admin" else role
    expected = {"employee": "group_leader", "group_leader": "sector_leader"}.get(role)
    if profile.manager_id and (not expected or user_role(profile.manager) != expected):
        profile.manager = None
    profile.save(update_fields=["role", "manager"])
    user.groups.clear()
    user.user_permissions.clear()
    if role in ("group_leader", "sector_leader"):
        group, _ = Group.objects.get_or_create(name=LEADER_GROUP)
        group.permissions.set(Permission.objects.filter(content_type__app_label="trainer", codename="review_attempt"))
        user.groups.add(group)
    for cache in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        user.__dict__.pop(cache, None)
