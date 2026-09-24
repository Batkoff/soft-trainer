"""Регрессии первого запуска и переноса проекта на другой компьютер."""
import argparse
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_demo import hostname, prepare, read_env


class PrepareDemoTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_fresh_copy_has_unique_secrets_and_credentials(self):
        settings = prepare(self.root)
        with tempfile.TemporaryDirectory() as other:
            another = prepare(Path(other))
        for key in ("SECRET_KEY", "POSTGRES_PASSWORD", "DEMO_PASSWORD"):
            self.assertGreater(len(settings[key]), 15)
            self.assertNotEqual(settings[key], another[key])
        self.assertIn(settings["DEMO_PASSWORD"], (self.root / ".demo-credentials.txt").read_text())
        self.assertEqual(settings["HTTP_BIND"], "127.0.0.1")

    def test_update_preserves_secrets_and_unknown_settings_exactly(self):
        prepare(self.root)
        path = self.root / ".env"
        with path.open("a") as stream:
            stream.write("# local setting\nCUSTOM_SETTING=keep-me\n")
        before = path.read_bytes()
        (self.root / ".demo-credentials.txt").unlink()
        prepare(self.root)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((self.root / ".demo-credentials.txt").exists())

    def test_copied_example_gets_secrets_but_keeps_api_key(self):
        (self.root / ".env").write_text("SECRET_KEY=\nPOSTGRES_PASSWORD=\nDEMO_PASSWORD=\nOPENROUTER_API_KEY=existing-test-value\n")
        settings = prepare(self.root)
        self.assertTrue(settings["SECRET_KEY"])
        self.assertEqual(settings["OPENROUTER_API_KEY"], "existing-test-value")

    def test_network_host_and_custom_port_work_together_without_reset(self):
        before = prepare(self.root)
        settings = prepare(self.root, host="192.168.1.20", port=8081)
        self.assertEqual(settings["SECRET_KEY"], before["SECRET_KEY"])
        self.assertEqual(settings["HTTP_BIND"], "0.0.0.0")
        self.assertIn("192.168.1.20", settings["ALLOWED_HOSTS"].split(","))
        self.assertIn("http://192.168.1.20:8081", settings["CSRF_TRUSTED_ORIGINS"].split(","))
        self.assertIn("http://localhost:8081", settings["CSRF_TRUSTED_ORIGINS"].split(","))
        self.assertEqual(read_env(self.root / ".env")["HTTP_PORT"], "8081")
        settings = prepare(self.root, port=8082)
        self.assertIn("http://192.168.1.20:8082", settings["CSRF_TRUSTED_ORIGINS"].split(","))

    def test_domain_host_enables_caddy_https_without_resetting_secrets(self):
        before = prepare(self.root)
        settings = prepare(self.root, host="ton-practice.ru", port=80)
        self.assertEqual(settings["SECRET_KEY"], before["SECRET_KEY"])
        self.assertEqual(settings["HTTP_BIND"], "0.0.0.0")
        self.assertEqual(settings["SITE_ADDRESS"], "ton-practice.ru")
        self.assertEqual(settings["HTTPS_PORT"], "443")
        self.assertEqual(settings["SECURE_COOKIES"], "1")
        self.assertIn("ton-practice.ru", settings["ALLOWED_HOSTS"].split(","))
        self.assertIn("https://ton-practice.ru", settings["CSRF_TRUSTED_ORIGINS"].split(","))
        self.assertIn("http://ton-practice.ru", settings["CSRF_TRUSTED_ORIGINS"].split(","))

    def test_invalid_port_does_not_write_settings(self):
        for port in (0, 65536):
            with self.assertRaises(ValueError):
                prepare(self.root, port=port)
        self.assertFalse((self.root / ".env").exists())

    def test_host_rejects_urls_and_accepts_ipv6(self):
        self.assertEqual(hostname("::1"), "[::1]")
        for value in ("https://example.org", "example.org:8080", "*", "example.org/path"):
            with self.assertRaises(argparse.ArgumentTypeError):
                hostname(value)


if __name__ == "__main__":
    unittest.main()
