import os
import unittest
from urllib.parse import quote
from unittest.mock import patch

from jobradar.diagnostics import safe_error


class DiagnosticTests(unittest.TestCase):
    def test_redacts_configured_credentials_and_encoded_forms(self):
        secret = "fake-credential/a?b=c"
        with patch.dict(os.environ, {"SUPABASE_SERVICE_ROLE_KEY": secret}, clear=True):
            self.assertEqual(safe_error(f"HTTP failed: {secret} {quote(secret, safe='')}"),
                             "HTTP failed: [redacted] [redacted]")

    def test_redacts_webhook_even_without_environment_value(self):
        with patch.dict(os.environ, {}, clear=True):
            result = safe_error(RuntimeError("503 for https://discord.com/api/webhooks/123/fake-token?wait=true"))
            self.assertEqual(result, "503 for [redacted Discord webhook]")

    def test_preserves_ordinary_provider_diagnostics(self):
        self.assertEqual(safe_error(RuntimeError("Workday page 2 failed: 429")),
                         "Workday page 2 failed: 429")
