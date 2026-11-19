"""A company whose name changes keeps one stored row per posting and nothing is announced again."""
import json
import os
import re
import unittest
from unittest import mock

os.environ.setdefault("SUPABASE_URL", "https://db.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

from jobradar import publish  # noqa: E402
from jobradar.models import Job  # noqa: E402


class Reply:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(self.status_code)


class FakeTable:
    """The jobs table behind PostgREST, just enough for publish(): filter, patch, merge-upsert."""

    def __init__(self, rows):
        self.rows = [dict(r, id=i + 1) for i, r in enumerate(rows)]
        self.queued = []

    def _match(self, params):
        out = self.rows
        for name, cond in params.items():
            if name in ("select", "order", "limit", "offset"):
                continue
            op, _, value = cond.partition(".")
            field = (lambda r: (r.get("source_payload") or {}).get("provider_slug")) if "provider_slug" in name else (lambda r, n=name: r.get(n))
            if op == "in":
                wanted = set(json.loads("[" + value[1:-1] + "]"))
                out = [r for r in out if field(r) in wanted]
            elif op == "eq":
                out = [r for r in out if (str(field(r)).lower() if isinstance(field(r), bool) else field(r)) == value]
            elif op == "neq":
                out = [r for r in out if field(r) != value]
        return out

    def get(self, url, headers=None, params=None, timeout=None):
        rows = self._match(params)
        start = int(params.get("offset", 0))
        return Reply([dict(r, **{publish.HASH_KEY: (r.get("source_payload") or {}).get(publish.HASH_KEY)}) for r in rows][start:start + int(params.get("limit", 1000))])

    def patch(self, url, headers=None, params=None, json=None, timeout=None):
        rows = self._match(params)
        for row in rows:
            row.update(json)
        return Reply([{"id": r["id"]} for r in rows])

    def post(self, url, headers=None, json=None, timeout=None):
        if "notifications" in url:
            self.queued.extend(json)
            return Reply([])
        for new in json:
            stored = next((r for r in self.rows if r["company"] == new["company"] and r["url"] == new["url"]), None)
            if stored:
                stored.update(new)
            else:
                self.rows.append(dict(new, id=len(self.rows) + 1))
        return Reply([])


def stored(company, n, active=True):
    return {"company": company, "url": f"https://jobs.ashbyhq.com/openai/{n}", "title": f"Role {n}", "is_active": active,
            "ats": "ashby", "posted_at": "2026-11-18", "source_payload": {"provider_slug": "openai"}}


def listed(company, n, slug="openai"):
    return Job(company=company, title=f"Role {n}", url=f"https://jobs.ashbyhq.com/{slug}/{n}", source="ashby",
               provider_slug=slug, posted_at="2026-11-18", description="text")


class RenamedCompany(unittest.TestCase):
    def run_publish(self, table, jobs, **kwargs):
        with mock.patch.object(publish, "_get", side_effect=table.get), \
                mock.patch.object(publish.requests, "patch", side_effect=table.patch), \
                mock.patch.object(publish.requests, "post", side_effect=table.post), \
                mock.patch.object(publish, "queue_new_jobs", side_effect=lambda u, k, rows: table.queued.extend(rows) or len(rows)), \
                mock.patch.object(publish, "notify_new_jobs", return_value=0), \
                mock.patch.object(publish, "supabase_env", return_value=("https://db.invalid", "k")):
            return publish.publish(jobs, **kwargs)

    def test_rows_under_the_old_name_are_renamed_not_stored_twice(self):
        table = FakeTable([stored("Openai", 1), stored("Openai", 2), stored("Openai", 3)])
        totals = self.run_publish(table, [listed("OpenAI", 1), listed("OpenAI", 2), listed("OpenAI", 4)])
        self.assertEqual(totals["renamed"], 3)
        self.assertEqual(sorted((r["company"], r["url"][-1], r["is_active"]) for r in table.rows),
                         [("OpenAI", "1", True), ("OpenAI", "2", True), ("OpenAI", "3", False), ("OpenAI", "4", True)])
        self.assertEqual([r["url"][-1] for r in table.queued], ["4"])   # only the posting that really is new

    def test_before_the_fix_every_posting_was_stored_and_queued_again(self):
        table = FakeTable([stored("Openai", 1), stored("Openai", 2)])
        with mock.patch.object(publish, "adopt_renamed_rows", return_value=0):
            self.run_publish(table, [listed("OpenAI", 1), listed("OpenAI", 2)])
        self.assertEqual(len(table.rows), 4)                         # two rows per posting
        self.assertEqual(len(table.queued), 2)                       # both announced again
        self.assertTrue(all(r["is_active"] for r in table.rows))     # the old rows stay open for ever

    def test_a_copy_already_stored_under_both_names_is_closed_not_renamed(self):
        table = FakeTable([stored("Openai", 1), stored("OpenAI", 1), stored("Openai", 2, active=False)])
        totals = self.run_publish(table, [listed("OpenAI", n) for n in (1, 3, 4, 5, 6, 7, 8)])
        state = sorted((r["company"], r["url"][-1], r["is_active"]) for r in table.rows)[:3]
        self.assertEqual(state, [("OpenAI", "1", True), ("OpenAI", "2", False), ("OpenAI", "3", True)])
        self.assertIn(("Openai", "1", False), [(r["company"], r["url"][-1], r["is_active"]) for r in table.rows])
        self.assertEqual(totals["renamed"], 1)
        self.assertEqual(sorted(r["url"][-1] for r in table.queued), ["3", "4", "5", "6", "7", "8"])

    def test_an_incomplete_run_renames_but_closes_nothing(self):
        table = FakeTable([stored("Openai", 1), stored("Openai", 3)])
        totals = self.run_publish(table, [listed("OpenAI", 1)], allow_deactivate=False)
        self.assertEqual(totals["renamed"], 2)
        self.assertTrue(all(r["is_active"] and r["company"] == "OpenAI" for r in table.rows))

    def test_an_ordinary_run_makes_no_look_up_by_source(self):
        table = FakeTable([stored("OpenAI", n) for n in range(1, 31)])
        with mock.patch.object(publish, "adopt_renamed_rows", return_value=0) as look_up:
            self.run_publish(table, [listed("OpenAI", n) for n in range(1, 36)])   # 5 new of 35
        look_up.assert_not_called()

    def test_a_source_that_reports_several_companies_is_left_alone(self):
        table = FakeTable([dict(stored("Other Co", 1), url="https://x.example/1", source_payload={"provider_slug": "feed"})])
        jobs = [Job(company="A", title="t", url="https://x.example/2", source="ashby", provider_slug="feed", description="d"),
                Job(company="B", title="t", url="https://x.example/3", source="ashby", provider_slug="feed", description="d")]
        totals = self.run_publish(table, jobs)
        self.assertEqual(totals["renamed"], 0)
        self.assertEqual(table.rows[0]["company"], "Other Co")

    def test_another_board_of_the_same_kind_is_not_touched(self):
        other = dict(stored("Ramp", 9), url="https://jobs.ashbyhq.com/ramp/9", source_payload={"provider_slug": "ramp"})
        table = FakeTable([other, stored("Openai", 1)])
        self.run_publish(table, [listed("OpenAI", 1)])
        self.assertEqual((table.rows[0]["company"], table.rows[0]["is_active"]), ("Ramp", True))


if __name__ == "__main__":
    unittest.main()
