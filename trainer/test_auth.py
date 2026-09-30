"""Регистрация по почте, роль без доступа и восстановление пароля."""
import re
import json
from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import AuditEvent, UserProfile
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
        created = AuditEvent.objects.get(action="registration_created")
        self.assertNotIn(user.email, json.dumps(created.details))
        self.assertTrue(created.object_id.startswith("registration:"))
        activation = re.search(r"http://testserver/activate/[^\s]+", mail.outbox[0].body).group(0)

        response = self.client.get(activation)
        self.assertRedirects(response, reverse("login"))
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(AuditEvent.objects.filter(action="registration_activated").exists())
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
        event = AuditEvent.objects.get(action="registration_rejected")
        self.assertNotIn("EXISTING@example.com", json.dumps(event.details))

    def test_mail_failure_rolls_back_account_and_records_safe_diagnostic(self):
        with patch("trainer.auth_views._send_activation_email", side_effect=OSError("secret smtp response")):
            with self.assertLogs("trainer.registration", level="WARNING") as logs:
                response = self.client.post(reverse("register"), {
                    "email": "fail.mail@example.com",
                    "password1": "long-registration-password-123",
                    "password2": "long-registration-password-123",
                })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Аккаунт не создан")
        self.assertContains(response, response["X-Request-ID"])
        self.assertEqual(response.content.decode().count(response["X-Request-ID"]), 1)
        self.assertFalse(get_user_model().objects.filter(email="fail.mail@example.com").exists())
        event = AuditEvent.objects.get(action="registration_email_failed")
        serialized = json.dumps(event.details)
        self.assertNotIn("fail.mail@example.com", serialized)
        self.assertNotIn("secret smtp response", serialized)
        self.assertIn("OSError", serialized)
        self.assertNotIn("fail.mail@example.com", "\n".join(logs.output))

    def test_unicode_smtp_failure_records_safe_diagnostic_without_values(self):
        with override_settings(EMAIL_HOST_USER="почта", EMAIL_HOST_PASSWORD="секрет"):
            error = UnicodeEncodeError("ascii", "секрет", 0, 1, "ordinal not in range(128)")
            with patch("trainer.auth_views.send_mail", side_effect=error):
                response = self.client.post(reverse("register"), {
                    "email": "ascii-recipient@example.com",
                    "password1": "long-registration-password-123",
                    "password2": "long-registration-password-123",
                })
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.get(action="registration_email_failed")
        diagnostic = json.dumps(event.details)
        self.assertIn("smtp_delivery", diagnostic)
        self.assertIn("smtp_password", diagnostic)
        self.assertIn("ascii", diagnostic)
        self.assertNotIn("секрет", diagnostic)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                       EMAIL_HOST="smtp.example.com", EMAIL_HOST_USER="smtp-user",
                       EMAIL_HOST_PASSWORD="пароль", DEFAULT_FROM_EMAIL="sender@example.com")
    def test_invalid_smtp_password_is_identified_before_sending_or_creating_account(self):
        with patch("trainer.auth_views.send_mail") as send:
            response = self.client.post(reverse("register"), {
                "email": "configuration-fail@example.com",
                "password1": "long-registration-password-123",
                "password2": "long-registration-password-123",
            })
        send.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(email="configuration-fail@example.com").exists())
        self.assertContains(response, response["X-Request-ID"], count=1)
        event = AuditEvent.objects.get(action="registration_email_failed")
        self.assertEqual(event.details["failure_stage"], "smtp_configuration")
        self.assertEqual(event.details["exception_metadata"]["setting"], "EMAIL_HOST_PASSWORD")
        self.assertEqual(event.details["exception_type"], "SMTP_CREDENTIAL_NON_ASCII")
        self.assertNotIn("пароль", json.dumps(event.details, ensure_ascii=False))
