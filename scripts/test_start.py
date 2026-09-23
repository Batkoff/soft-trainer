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


class DockerReadinessTests(unittest.TestCase):
    def test_success_checks_server_without_docker_info(self):
        with patch.object(start.subprocess, "run", side_effect=[result(), result()]) as run:
            start.ensure_docker(wait_seconds=0)
        self.assertEqual(run.call_args.args[0], ["docker", "version", "--format", "{{.Server.Version}}"])

    def test_initial_timeout_is_retried(self):
        with patch.object(start.subprocess, "run", side_effect=[result(), subprocess.TimeoutExpired("docker", 10), result()]) as run, patch.object(start.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            start.ensure_docker(wait_seconds=30)
        self.assertEqual(run.call_count, 3)

    def test_daemon_error_is_reported_and_secret_is_redacted(self):
        with patch.object(start.subprocess, "run", side_effect=[result(), result(1, "Cannot connect: unit-secret")]), patch.object(start, "read_env", return_value={"POSTGRES_PASSWORD": "unit-secret"}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                start.ensure_docker(wait_seconds=0)
        self.assertIn("Cannot connect", str(raised.exception))
        self.assertNotIn("unit-secret", str(raised.exception))

    def test_empty_server_version_is_not_a_success(self):
        empty = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(start.subprocess, "run", side_effect=[result(), empty]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                start.ensure_docker(wait_seconds=0)

    def test_settings_are_prepared_before_probe_and_no_compose_logs_without_engine(self):
        from unittest.mock import Mock
        events = Mock()
        with patch.object(start.sys, "argv", ["start.py"]), patch.object(start.shutil, "which", return_value="docker"), patch.object(start, "prepare", return_value={}) as prepare, patch.object(start, "ensure_docker", side_effect=RuntimeError("daemon unavailable")) as docker, patch.object(start, "show_failure_logs") as logs, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            events.attach_mock(prepare, "prepare")
            events.attach_mock(docker, "docker")
            with self.assertRaises(SystemExit):
                start.main()
        self.assertEqual([call[0] for call in events.mock_calls], ["prepare", "docker"])
        logs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
