"""Диагностика почты не отправляет писем и не раскрывает SMTP-секреты."""
from io import StringIO
from smtplib import SMTPAuthenticationError
from unittest.mock import MagicMock, patch

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings

from .email_configuration import validate_smtp_configuration


@override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                   EMAIL_HOST="smtp.example.com", EMAIL_HOST_USER="smtp-user",
                   EMAIL_HOST_PASSWORD="test-only-password", DEFAULT_FROM_EMAIL="sender@example.com")
class SMTPDiagnosticTests(SimpleTestCase):
    def test_command_only_opens_and_closes_connection(self):
        output = StringIO()
        connection = MagicMock()
        with patch("trainer.management.commands.check_smtp.get_connection", return_value=connection):
            call_command("check_smtp", stdout=output)
        connection.__enter__.assert_called_once()
        connection.__exit__.assert_called_once()
        connection.send_messages.assert_not_called()
        self.assertIn("Авторизация успешна", output.getvalue())

    @override_settings(EMAIL_HOST_PASSWORD="smtp-secret-кириллица")
    def test_invalid_credentials_do_not_connect_or_print_the_value(self):
        with patch("trainer.management.commands.check_smtp.get_connection") as connect:
            with self.assertRaises(CommandError) as captured:
                call_command("check_smtp")
        connect.assert_not_called()
        self.assertIn("EMAIL_HOST_PASSWORD", str(captured.exception))
        self.assertNotIn("smtp-secret-кириллица", str(captured.exception))

    def test_provider_response_does_not_expose_secrets(self):
        connection = MagicMock()
        connection.__enter__.side_effect = SMTPAuthenticationError(535, b"secret-provider-response")
        with patch("trainer.management.commands.check_smtp.get_connection", return_value=connection):
            with self.assertRaises(CommandError) as captured:
                call_command("check_smtp")
        self.assertIn("535", str(captured.exception))
        self.assertNotIn("secret-provider-response", str(captured.exception))

    @override_settings(DEFAULT_FROM_EMAIL="Тон <sender@example.com>")
    def test_cyrillic_sender_display_name_is_allowed(self):
        validate_smtp_configuration()
