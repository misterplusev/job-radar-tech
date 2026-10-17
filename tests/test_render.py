"""Render smoke test — proves the workflow's dashboard artifact actually gets written.

Regression guard: `run.py render` used to swallow every exception, print
"[render] skipped" and exit 0, while querying `job_scores` — a table the module
schema never created. Every hourly run was green and uploaded nothing.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.dashboard import render_dashboard
from jobradar.models import Job
from jobradar.storage import SQLiteBackend


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.store = SQLiteBackend(self.tmp.name)
        self.store.upsert_jobs([
            Job(company="Apptronik", title="Controls Engineer - Actuation",
                url="https://example.invalid/a", location="Austin, TX",
                description="actuation controls", source="greenhouse"),
            Job(company="Figure AI", title="Robotics Software Engineer",
                url="https://example.invalid/b", location="Berkeley, CA",
                description="humanoid robotics", source="greenhouse"),
            Job(company="Old Corp", title="Retired Role", url="https://example.invalid/c",
                location="Remote", description="x", source="greenhouse",
                is_active=False),
        ])

    def test_writes_non_empty_artifact(self):
        out = Path(tempfile.mkdtemp()) / "out" / "dashboard.html"
        path = render_dashboard(None, self.store, str(out))
        self.assertTrue(out.exists(), "render must create the artifact")
        html = out.read_text(encoding="utf-8")
        self.assertGreater(len(html), 500)
        self.assertNotIn("__DATA__", html, "unresolved __DATA__ placeholder")
        self.assertNotIn("__HAS_SCORE__", html, "unresolved __HAS_SCORE__ placeholder")

    def test_excludes_inactive_jobs(self):
        out = Path(tempfile.mkdtemp()) / "dashboard.html"
        render_dashboard(None, self.store, str(out))
        html = out.read_text(encoding="utf-8")
        self.assertIn("Apptronik", html)
        self.assertNotIn("Old Corp", html)

    def test_unscored_rows_render_without_fit_column_crashing(self):
        out = Path(tempfile.mkdtemp()) / "dashboard.html"
        render_dashboard(None, self.store, str(out))
        html = out.read_text(encoding="utf-8")
        # module pipeline never writes job_scores -> HAS_SCORE must be false
        self.assertIn("HAS_SCORE=false", html)


if __name__ == "__main__":
    unittest.main()