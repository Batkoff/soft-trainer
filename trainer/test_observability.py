"""HTTP diagnostics must not retain one-time account tokens from URL paths."""
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from .observability import RequestLogMiddleware


class RequestPathRedactionTests(SimpleTestCase):
    def test_activation_and_password_reset_tokens_are_redacted(self):
        for path, prefix, secret in (
            ("/activate/123/activation-secret/", "/activate/[redacted]/", "activation-secret"),
            ("/password-reset/123/reset-secret/", "/password-reset/[redacted]/", "reset-secret"),
        ):
            request = RequestFactory().get(path)
            request.user = AnonymousUser()
            middleware = RequestLogMiddleware(lambda _request: HttpResponse("ok"))
            with self.assertLogs("trainer.http", level="INFO") as logs:
                response = middleware(request)
            serialized = "\n".join(logs.output)
            self.assertEqual(response["X-Request-ID"], request.request_id)
            self.assertIn(prefix, serialized)
            self.assertNotIn(secret, serialized)
