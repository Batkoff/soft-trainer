"""Регистрация по почте, роль без доступа и восстановление пароля."""
import re
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import UserProfile
from .people import is_manager, user_role


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    ALLOWED_HOSTS=["testserver"],
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AccountEmailTests(TestCase):
    def setUp(self):
        mail.outbox.clear()

    def test_registration_requires_confirmation_and_starts_without_role(self):
        response = self.client.post(reverse("register"), {
            "email": "new.person@example.com",
            "display_name": "Новый человек",
            "password1": "long-registration-password-123",
            "password2": "long-registration-password-123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Письмо отправлено")
        user = get_user_model().objects.get(email="new.person@example.com")
        self.assertFalse(user.is_active)
        self.assertEqual(user_role(user), "unassigned")
        self.assertFalse(is_manager(user))
        self.assertFalse(user.is_staff)
        self.assertEqual(mail.outbox[0].to, [user.email])
        activation = re.search(r"http://testserver/activate/[^\s]+", mail.outbox[0].body).group(0)

        response = self.client.get(activation)
        self.assertRedirects(response, reverse("login"))
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user_role(user), "unassigned")
        self.assertTrue(self.client.login(username=user.email, password="long-registration-password-123"))
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("sandbox")).status_code, 403)

    def test_activation_link_is_single_use(self):
        self.client.post(reverse("register"), {
            "email": "single.use@example.com",
            "password1": "long-registration-password-123",
            "password2": "long-registration-password-123",
        })
        activation = re.search(r"http://testserver/activate/[^\s]+", mail.outbox[0].body).group(0)
        self.assertEqual(self.client.get(activation).status_code, 302)
        self.assertEqual(self.client.get(activation).status_code, 400)

    def test_password_reset_email_changes_password(self):
        user = get_user_model().objects.create_user(
            username="mail-user", email="mail-user@example.com", password="old-password-123",
        )
        response = self.client.post(reverse("password_reset"), {"email": user.email})
        self.assertRedirects(response, reverse("password_reset_done"))
        reset_url = re.search(r"http://testserver/password-reset/[^\s]+", mail.outbox[0].body).group(0)
        response = self.client.get(reset_url)
        self.assertEqual(response.status_code, 302)
        reset_path = urlparse(response.url).path
        response = self.client.post(reset_path, {
            "new_password1": "new-password-12345",
            "new_password2": "new-password-12345",
        })
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.assertTrue(self.client.login(username=user.email, password="new-password-12345"))
        self.client.logout()
        self.assertFalse(self.client.login(username=user.email, password="old-password-123"))

    def test_duplicate_email_is_rejected_without_sending_message(self):
        get_user_model().objects.create_user(
            username="existing", email="existing@example.com", password="old-password-123",
        )
        response = self.client.post(reverse("register"), {
            "email": "EXISTING@example.com",
            "password1": "long-registration-password-123",
            "password2": "long-registration-password-123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "уже зарегистрирован")
        self.assertEqual(len(mail.outbox), 0)
