"""Run in cloud CI only. All transport and clocks are fake."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from email.utils import formatdate
from unittest.mock import Mock, patch
try:
    from jobradar import workday_http as http
except ModuleNotFoundError:
    from lib import workday_http as http
import requests

URL = "https://example.wd1.myworkdayjobs.com/wday/cxs/example/External/jobs"

class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

def response(status=200, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r.url = URL
    r.headers.update(headers or {})
    r._content = (json.dumps(body if body is not None else {"total": 0, "jobPostings": []})).encode()
    return r

class WorkdayPolicyTests(unittest.TestCase):
    def setUp(self):
        http._HOSTS.clear()
        self.clock, self.events = Clock(), []
    def client(self, replies, budget=175):
        send = Mock(side_effect=replies)
        client = http.WorkdayHTTP(send, source="test", deadline=self.clock()+budget,
                 clock=self.clock, sleep=self.clock.sleep, jitter=lambda: 0, logger=self.events.append)
        return client, send
    def test_read_only_post_recovers_and_records_attempts(self):
        client, send = self.client([response(429), response(503), response()])
        client.request("POST", URL, json={"offset": 20})
        self.assertEqual(self.clock.waits, [5, 10])
        self.assertEqual(send.call_count, 3)
        self.assertEqual([e["decision"] for e in self.events], ["retry", "retry", "success"])
        self.assertTrue(all(e["offset"] == 20 for e in self.events))
        self.assertTrue(all(c.kwargs["json"] == {"offset": 20} for c in send.call_args_list))
    def test_numeric_retry_after_is_not_shortened(self):
        client, _ = self.client([response(429, headers={"Retry-After": "120"}), response()])
        client.request("POST", URL)
        self.assertEqual(self.clock.waits, [120])
    def test_http_date_retry_after(self):
        with patch.object(http.time, "time", return_value=1700000000):
            raw = formatdate(1700000090, usegmt=True)
            client, _ = self.client([response(429, headers={"Retry-After": raw}), response()])
            client.request("POST", URL)
        self.assertEqual(self.clock.waits, [90])
    def test_long_wait_defers_without_early_retry(self):
        client, send = self.client([response(429, headers={"Retry-After": "600"})])
        with self.assertRaises(http.WorkdayDeferred):
            client.request("POST", URL)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(self.events[-1]["decision"], "deferred")
    def test_repeated_429_is_bounded(self):
        client, send = self.client([response(429)]*5)
        with self.assertRaises(http.WorkdayDeferred):
            client.request("POST", URL)
        self.assertEqual(send.call_count, 5)
        self.assertEqual(self.clock.waits, [5, 10, 20, 40])
    def test_forbidden_is_terminal(self):
        client, send = self.client([response(403)])
        with self.assertRaises(requests.HTTPError):
            client.request("POST", URL)
        self.assertEqual(send.call_count, 1)
    def test_write_posts_are_never_sent(self):
        client, send = self.client([])
        for url in (URL.replace("/jobs", "/apply"), "https://discord.com/api/webhooks/test"):
            with self.assertRaises(ValueError):
                client.request("POST", url)
        send.assert_not_called()
    def test_successful_pages_are_paced(self):
        client, send = self.client([response(), response()])
        client.request("POST", URL)
        client.request("POST", URL)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.clock.waits, [0.25])
    def test_same_host_clients_share_cooldown(self):
        one, _ = self.client([response(429, headers={"Retry-After": "600"})])
        with self.assertRaises(http.WorkdayDeferred):
            one.request("POST", URL)
        two, send = self.client([])
        with self.assertRaises(http.WorkdayDeferred):
            two.request("POST", URL)
        send.assert_not_called()
    def test_non_json_and_timeout_recover(self):
        bad = response()
        bad._content = b"<html>temporarily unavailable</html>"
        client, _ = self.client([bad, requests.Timeout(), response()])
        client.request("POST", URL)
        self.assertEqual([e["reason"] for e in self.events[:2]], ["non-JSON response", "Timeout"])
    def test_context_deadline_is_used(self):
        with http.source_budget(1005):
            client = http.WorkdayHTTP(Mock(), source="test", clock=self.clock)
        self.assertEqual(client.deadline, 1005)
    def test_logs_are_redacted_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
             "ABET_BASE_DIR": directory, "FAKE_SECRET_KEY": "super-secret-value"}), redirect_stderr(io.StringIO()):
            clean = http._clean("<html>super-secret-value somebody@example.com https://example.com/token</html>")
            http.emit({"decision": "retry", "response_excerpt": clean})
            text = http.diagnostic_path().read_text()
            self.assertNotIn("super-secret-value", text)
            self.assertNotIn("somebody@example.com", text)
            self.assertNotIn("/token", text)
            self.assertEqual(http.diagnostic_summary()["attempts_recorded"], 1)
            with patch.object(http, "MAX_LOG_BYTES", 1):
                http.emit({"decision": "retry"})
            self.assertEqual(http.diagnostic_summary()["file_events_dropped"], 1)
        http._LOG_DROPPED = 0
