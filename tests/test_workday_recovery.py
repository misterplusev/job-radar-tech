"""Provider integration: fake HTTP and clocks; run only in cloud CI."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import Mock, patch
from jobradar.providers import workday
from jobradar import workday_http as http
from test_workday_http import Clock, response

def page(start=0, total=1, count=1):
    return response(body={"total": total, "jobPostings": [
        {"title": f"Job {i}", "externalPath": f"/job/{i}"} for i in range(start, start+count)]})

class WorkdayRecovery(unittest.TestCase):
    def setUp(self):
        http._HOSTS.clear()
        self.clock, self.events = Clock(), []
    def run_provider(self, replies):
        send = Mock(side_effect=replies)
        client = http.WorkdayHTTP(send, source="test", deadline=self.clock()+175,
             clock=self.clock, sleep=self.clock.sleep, jitter=lambda: 0, logger=self.events.append)
        with patch.object(workday, "_new_client", return_value=client):
            jobs = workday.WorkdayProvider("bostondynamics").fetch("test")
        return jobs, send
    def test_recovery_does_not_duplicate_first_page(self):
        jobs, send = self.run_provider([page(0,40,20), response(429), page(20,0,20)])
        self.assertEqual(len({j.url for j in jobs}), 40)
        self.assertEqual([c.kwargs["json"]["offset"] for c in send.call_args_list], [0,20,20])
    def test_failed_later_page_does_not_return_partial_source(self):
        with self.assertRaises(http.WorkdayDeferred):
            self.run_provider([page(0,40,20)] + [response(429)]*5)
    def test_missing_schema_and_early_empty_fail(self):
        for replies in ([response(body={"error":"denied"})], [page(0,40,20), page(20,0,0)]):
            with self.subTest(replies=replies), self.assertRaises(ValueError):
                self.run_provider(replies)
            http._HOSTS.clear()
    def test_verified_empty_remains_valid(self):
        jobs, _ = self.run_provider([page(0,0,0)])
        self.assertEqual(jobs, [])
    def test_recovered_child_diagnostics_are_forwarded(self):
        from jobradar.fetch_worker import fetch_provider
        def complete(args, **kwargs):
            Path(args[-1]).write_text("[]")
            self.assertIn("WORKDAY_SOURCE_SECONDS", kwargs["env"])
            return SimpleNamespace(returncode=0, stderr='[WORKDAY_HTTP] {"decision":"retry"}\n')
        captured = io.StringIO()
        provider = SimpleNamespace(source="workday", slug="bostondynamics")
        with patch("jobradar.fetch_worker.subprocess.run", side_effect=complete), redirect_stderr(captured):
            self.assertEqual(fetch_provider(provider, "test"), [])
        self.assertIn('"decision":"retry"', captured.getvalue())
