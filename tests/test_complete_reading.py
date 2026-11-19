"""Only a complete reading of a source may close the jobs it does not list.

From the Codex review of 2026-11-19 (T09, RAD-03): a Greenhouse list that does not match the board's own
total was accepted, and Amazon skipped the rows after a short page.
"""
import os
import re
import unittest
from unittest import mock

os.environ.setdefault("JOBRADAR_MODULE", "robotics")

from jobradar.providers import amazon, greenhouse


class Reply:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def posting(n):
    return {"title": f"Engineer {n}", "absolute_url": f"https://boards.greenhouse.io/x/jobs/{n}",
            "location": {"name": "Remote"}, "content": "", "first_published": "2026-11-01T00:00:00-04:00"}


class CompleteReading(unittest.TestCase):
    def test_greenhouse_list_shorter_than_its_total_is_refused(self):
        payload = {"name": "X", "jobs": [posting(1)], "meta": {"total": 3}}
        with mock.patch.object(greenhouse, "http_get", return_value=Reply(payload)):
            with self.assertRaises(ValueError) as ctx:
                greenhouse.GreenhouseProvider("x").fetch("run")
        self.assertIn("reported 3", str(ctx.exception))

    def test_greenhouse_matching_total_is_accepted(self):
        payload = {"name": "X", "jobs": [posting(1), posting(2)], "meta": {"total": 2}}
        with mock.patch.object(greenhouse, "http_get", return_value=Reply(payload)):
            self.assertEqual(len(greenhouse.GreenhouseProvider("x").fetch("run")), 2)

    def test_amazon_short_page_does_not_skip_rows(self):
        rows = [{"job_id": str(n), "job_path": f"/en/jobs/{n}", "title": f"Robotics role {n}"} for n in range(120)]

        def fake_get(url, **_kwargs):
            offset = int(re.search(r"offset=(\d+)", url).group(1))
            return Reply({"jobs": rows[offset:offset + 60], "hits": len(rows)})  # the site returns 60, not 100

        with mock.patch.object(amazon, "http_get", side_effect=fake_get), \
                mock.patch.object(amazon, "QUERIES", ["robotics"]), mock.patch.object(amazon.time, "sleep"):
            provider = [cls for name, cls in vars(amazon).items() if name.endswith("Provider") and name != "Provider"][0]
            jobs = provider("robotics").fetch("run")
        self.assertEqual(len(jobs), 120)


if __name__ == "__main__":
    unittest.main()
