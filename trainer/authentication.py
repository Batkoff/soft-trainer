"""Вход по логину или подтверждённому адресу электронной почты."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class EmailOrUsernameBackend(ModelBackend):
    """Стандартный backend прав Django с дополнительным поиском по email."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or password is None:
            return None
        User = get_user_model()
        identity = username.strip()
        user = User.objects.filter(
            Q(username__iexact=identity) | Q(email__iexact=identity)
        ).order_by("pk").first()
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
