"""Проверки мастера без реальных ключей и без обращения к API."""
import contextlib
import getpass
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import configure_ai

class ConfigureAITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.original = "SECRET_KEY=test-placeholder\nEVALUATOR_BACKEND=demo\nOPENROUTER_API_KEY=previous-placeholder\n"
        (self.root / ".env").write_text(self.original, encoding="utf-8")
        root_patch = patch.object(configure_ai, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def test_unsupported_terminal_exits_before_hidden_input_without_changes(self):
        with patch("builtins.input", return_value="2"), patch.object(configure_ai, "supports_hidden_input", return_value=False), \
                patch("getpass.getpass") as secret, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
            configure_ai.main()
        secret.assert_not_called()
        self.assertIn("OPENROUTER_API_KEY=", str(error.exception))
        self.assertIn("notepad .env", str(error.exception))
        self.assertEqual((self.root / ".env").read_text(), self.original)

    def test_supported_terminal_saves_settings_without_printing_key(self):
        output = io.StringIO()
        with patch("builtins.input", return_value="2"), patch.object(configure_ai, "supports_hidden_input", return_value=True), \
                patch("getpass.getpass", return_value="sk-or-test-placeholder"), contextlib.redirect_stdout(output):
            configure_ai.main()
        result = (self.root / ".env").read_text()
        self.assertIn("SECRET_KEY=test-placeholder", result)
        self.assertIn("EVALUATOR_BACKEND=openrouter", result)
        self.assertEqual(result.count("OPENROUTER_API_KEY="), 1)
        self.assertIn("Символы и звёздочки не отображаются", output.getvalue())
        self.assertNotIn("sk-or-test-placeholder", output.getvalue())
        self.assertFalse((self.root / ".env.ai-tmp").exists())

    def test_getpass_warning_does_not_fall_back_to_visible_input(self):
        with patch("builtins.input", return_value="1"), patch.object(configure_ai, "supports_hidden_input", return_value=True), \
                patch("getpass.getpass", side_effect=getpass.GetPassWarning), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
            configure_ai.main()
        self.assertIn("OPENAI_API_KEY=", str(error.exception))
        self.assertEqual((self.root / ".env").read_text(), self.original)

    def test_demo_mode_never_requests_hidden_input(self):
        with patch("builtins.input", return_value="3"), patch("getpass.getpass") as secret, contextlib.redirect_stdout(io.StringIO()):
            configure_ai.main()
        secret.assert_not_called()

if __name__ == "__main__":
    unittest.main()
