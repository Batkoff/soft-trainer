"""Первый запуск, повторный запуск и восстановление пароля сохранённого тома."""
import contextlib
import io
import subprocess
import unittest
from unittest.mock import patch

from scripts import start


def result(code=0, error=""):
    return subprocess.CompletedProcess([], code, stdout="1\n" if not code else "", stderr=error)


class DatabaseStartupTests(unittest.TestCase):
    def run_database(self, results):
        with patch.object(start, "compose", side_effect=results) as command, contextlib.redirect_stdout(io.StringIO()):
            start.ensure_database()
        return command.call_args_list

    def test_healthy_database_does_not_change_password(self):
        calls = self.run_database([result(), result()])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[-1], "db")
        self.assertEqual(calls[1].args, start.APP_PROBE)

    def test_wrong_password_is_repaired_then_verified(self):
        calls = self.run_database([result(), result(42, "password authentication failed"), result(), result(), result()])
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[3].args[-1], start.SYNC_PASSWORD)
        self.assertEqual(calls[4].args, start.APP_PROBE)
        self.assertTrue(all("-T" in call.args for call in calls[1:]))
        self.assertFalse(any("down" in call.args for call in calls))

    def test_unrelated_failure_does_not_reset_password(self):
        with self.assertRaisesRegex(RuntimeError, "не прошёл проверку"), patch.object(start, "compose", side_effect=[result(), result(2, "connection refused")]) as command, contextlib.redirect_stdout(io.StringIO()):
            start.ensure_database()
        self.assertEqual(command.call_count, 2)

    def test_local_access_denied_does_not_weaken_authentication(self):
        with self.assertRaisesRegex(RuntimeError, "запрещено локальное"), patch.object(start, "compose", side_effect=[result(), result(42, "password authentication failed"), result(2)]) as command, contextlib.redirect_stdout(io.StringIO()):
            start.ensure_database()
        self.assertEqual(command.call_count, 3)

    def test_repair_failure_stops_startup(self):
        with self.assertRaisesRegex(RuntimeError, "Не удалось согласовать"), patch.object(start, "compose", side_effect=[result(), result(42, "password authentication failed"), result(), result(2)]), contextlib.redirect_stdout(io.StringIO()):
            start.ensure_database()

    def test_secret_is_not_a_command_argument(self):
        self.assertIn('$POSTGRES_PASSWORD', start.SYNC_PASSWORD)
        self.assertIn(r'\password trainer', start.SYNC_PASSWORD)
        self.assertNotIn('ALTER ROLE', start.SYNC_PASSWORD)


class AdminAccessTests(unittest.TestCase):
    def test_verified_password_replaces_stale_credentials(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, patch.object(start, "ROOT", Path(directory)), patch.object(start, "compose", return_value=subprocess.CompletedProcess([], 0, '{"matches":true}\n', "")):
            path = Path(directory) / ".demo-credentials.txt"
            path.write_text("stale-password")
            start.verify_admin_access({"DEMO_PASSWORD": "verified-test-password"})
            self.assertIn("verified-test-password", path.read_text())
            self.assertNotIn("stale-password", path.read_text())

    def test_mismatch_reports_recovery_without_guessing_password(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory, patch.object(start, "ROOT", Path(directory)), patch.object(start, "compose", return_value=subprocess.CompletedProcess([], 0, '{"matches":false}\n', "")) as command, contextlib.redirect_stdout(io.StringIO()):
            start.verify_admin_access({"DEMO_PASSWORD": "incorrect-test-password"})
            text = (Path(directory) / ".demo-credentials.txt").read_text()
            self.assertNotIn("incorrect-test-password", text)
            self.assertIn("--reset-admin", text)
            self.assertNotIn("--reset-admin", command.call_args.args)

    def test_recovery_preserves_other_secrets_and_uses_stdin(self):
        import tempfile
        from pathlib import Path
        password = "new-test-password-for-recovery"
        with tempfile.TemporaryDirectory() as directory, patch.object(start, "ROOT", Path(directory)), patch.object(start.secrets, "token_urlsafe", return_value=password), patch.object(start, "compose", return_value=subprocess.CompletedProcess([], 0, '{"matches":true}\n', "")) as command:
            path = Path(directory) / ".env"
            path.write_text("SECRET_KEY=preserved-test-key\n DEMO_PASSWORD=old-test-password\n")
            start.verify_admin_access({"DEMO_PASSWORD": "old-test-password"}, reset=True)
            self.assertEqual(start.read_env(path)["DEMO_PASSWORD"], password)
            self.assertEqual(start.read_env(path)["SECRET_KEY"], "preserved-test-key")
            self.assertNotIn(password, command.call_args.args)
            self.assertEqual(command.call_args.kwargs["input"], password)


if __name__ == "__main__":
    unittest.main()
