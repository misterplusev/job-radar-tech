"""Guardrails from the 2026-11-11/12 outage. These run before EVERY scrape (pipeline.yml,
"Regression checks"), so a change that reintroduces the cause stops the run with the reason.

What happened: the schedule was changed from a few runs a day to every hour for three
scrapers at once, every run rewrote every stored row, and every run applied the schema and
reloaded the REST API's schema cache. The shared FREE Supabase database (about 0.4 GB of
memory, 500 MB size limit) could not keep up and the REST API went down for 36 hours,
taking all four job scrapers and their boards with it.

If one of these tests fails, do not edit the test to make it pass. Read
docs/job-scrapers/RUNBOOK.md ("Limits you must not cross") first.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / '.github' / 'workflows').glob('*.yml'))
WORKER = ROOT / 'runtime' / 'cloudflare' / 'worker.mjs'
RUNBOOK = 'See docs/job-scrapers/RUNBOOK.md, "Limits you must not cross".'


class OneSchedulerOnly(unittest.TestCase):
    def test_no_workflow_has_its_own_timer(self):
        """The Cloudflare scheduler is the only clock. A GitHub cron here would start
        extra runs the scheduler cannot see or stagger."""
        self.assertTrue(WORKFLOWS)
        for path in WORKFLOWS:
            text = path.read_text(encoding='utf-8')
            self.assertIsNone(re.search(r'^\s*schedule\s*:', text, re.M), f'{path.name} has a schedule trigger. {RUNBOOK}')
            self.assertIsNone(re.search(r'^\s*-\s*cron\s*:', text, re.M), f'{path.name} has a cron line. {RUNBOOK}')

    def test_scrape_workflows_never_apply_schema(self):
        """Schema changes are a separate, manual workflow (apply-schema.yml)."""
        for path in WORKFLOWS:
            if path.name == 'apply-schema.yml':
                continue
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('apply_schema', text, f'{path.name} applies schema during a scrape. {RUNBOOK}')
            self.assertNotIn('NOTIFY pgrst', text, f'{path.name} reloads the REST schema cache. {RUNBOOK}')


@unittest.skipUnless(WORKER.exists(), 'scheduler source lives in the job-radar repository')
class SchedulerCadence(unittest.TestCase):
    def modules(self):
        block = re.search(r'export const MODULES = \[(.*?)\n\];', WORKER.read_text(encoding='utf-8'), re.S).group(1)
        found = []
        for line in block.splitlines():
            if 'id:' not in line:
                continue
            found.append({'id': re.search(r"id: '([a-z_]+)'", line).group(1),
                          'every': int(re.search(r'everyHours: (\d+)', line).group(1)),
                          'offset': int(re.search(r'hourOffset: (\d+)', line).group(1))})
        return found

    def test_all_four_scrapers_are_on_the_one_scheduler(self):
        self.assertEqual(sorted(m['id'] for m in self.modules()), ['robotics', 'sports_teams', 'sportsbook', 'tech'])

    def test_no_scraper_runs_more_often_than_every_four_hours(self):
        for m in self.modules():
            self.assertGreaterEqual(m['every'], 4, f"{m['id']} is scheduled every {m['every']} h. {RUNBOOK}")
            self.assertEqual(24 % m['every'], 0)

    def test_no_two_scrapers_share_an_hour(self):
        used = {}
        for m in self.modules():
            for hour in range(m['offset'], 24, m['every']):
                self.assertNotIn(hour, used, f"{m['id']} and {used.get(hour)} both run in UTC hour {hour}. {RUNBOOK}")
                used[hour] = m['id']

    def test_scheduler_checks_the_database_before_starting_runs(self):
        text = WORKER.read_text(encoding='utf-8')
        self.assertIn('databaseCheck', text, f'The database circuit breaker was removed. {RUNBOOK}')
        self.assertIn('held_database_down', text)


class WriteVolumeLimits(unittest.TestCase):
    def test_backfill_stays_small_and_descriptions_are_stored_once(self):
        text = (ROOT / 'jobradar' / 'publish.py').read_text(encoding='utf-8')
        budget = int(re.search(r'^BACKFILL_PER_RUN = (\d+)', text, re.M).group(1))
        self.assertLessEqual(budget, 1500, f'BACKFILL_PER_RUN={budget} rewrites too many rows per run. {RUNBOOK}')
        self.assertNotIn('"description_raw": job.description_raw', text, f'Descriptions are stored once. {RUNBOOK}')
        self.assertIn('TOUCH_COLUMNS', text, f'Unchanged rows must get the light refresh, not a full rewrite. {RUNBOOK}')


if __name__ == '__main__':
    unittest.main()
