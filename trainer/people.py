"""Три понятные роли пилота. У руководителя нет управления пользователями и конкурсами."""
from django.contrib.auth.models import Group, Permission

ROLE_CHOICES = [("employee", "Сотрудник"), ("leader", "Руководитель"), ("admin", "Администратор")]
LEADER_GROUP = "Руководители тренажёра"

def can_review(user):
    return user.is_active and user.is_staff and user.has_perm("trainer.review_attempt")

def user_role(user):
    return "admin" if user.is_superuser else "leader" if user.is_staff else "employee"

def display_name(user):
    profile = getattr(user, "profile", None)
    return (profile.display_name if profile else "") or user.get_full_name() or user.username

def apply_role(user, role):
    """Роли полностью задают права: старые индивидуальные разрешения не остаются скрыто."""
    if role not in dict(ROLE_CHOICES):
        raise ValueError("Неизвестная роль")
    user.is_staff = role in ("leader", "admin")
    user.is_superuser = role == "admin"
    user.save(update_fields=["is_staff", "is_superuser"])
    user.groups.clear()
    user.user_permissions.clear()
    if role == "leader":
        group, _ = Group.objects.get_or_create(name=LEADER_GROUP)
        group.permissions.set(Permission.objects.filter(content_type__app_label="trainer", codename__in=[
            "view_exercise", "view_contest", "view_attempt", "review_attempt",
            "view_calibrationcase", "view_calibrationrun",
        ]))
        user.groups.add(group)
    for cache in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        user.__dict__.pop(cache, None)
