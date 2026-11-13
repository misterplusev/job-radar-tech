"""Offline checks for the reduced write volume. HTTP is always replaced by a fake."""
import unittest
from unittest.mock import patch

from test_reliability import MODULES, load_publisher
from test_reliability import make_job as _make_job

URL1 = 'https://careers.example/jobs/1'
# Employers report a fixed posting date; the shared fixture stamps the current time
# on every call, which would look like an edit on each run.
POSTED_AT = _make_job(0).posted_at


def make_job(index):
    job = _make_job(index)
    job.posted_at = POSTED_AT
    return job


def job_writes(call_list):
    """Row batches POSTed to the jobs table (not the queue, receipts or Discord)."""
    out = []
    for call in call_list:
        url = call.args[0]
        if 'discord' in url or '_job_notifications' in url or url.endswith('_job_scrape_runs'):
            continue
        out.extend(call.kwargs['json'])
    return out


def publish_and_capture(ns, db, jobs, **kwargs):
    with patch.object(db, 'post', wraps=db.post) as post:
        totals = ns['publish'](jobs, **kwargs)
    return totals, job_writes(post.call_args_list)


class WriteVolumeTests(unittest.TestCase):
    def test_new_posting_is_written_in_full_with_a_content_hash(self):
        ns, db = load_publisher(MODULES[0])
        totals, rows = publish_and_capture(ns, db, [make_job(1)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['description'], 'Operations')
        self.assertEqual(len(rows[0]['source_payload'][ns['HASH_KEY']]), 32)
        self.assertEqual(totals['written_new'], 1)

    def test_unchanged_posting_only_refreshes_tracking_columns(self):
        ns, db = load_publisher(MODULES[0])
        ns['publish']([make_job(1)])
        stored = dict(db.jobs['Employer', URL1])
        totals, rows = publish_and_capture(ns, db, [make_job(1)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), set(ns['TOUCH_COLUMNS']))
        after = db.jobs['Employer', URL1]
        self.assertEqual(after['description'], stored['description'])
        self.assertEqual(after['source_payload'], stored['source_payload'])
        self.assertEqual((totals['touched'], totals['written_changed']), (1, 0))
        self.assertEqual(len(db.sends), 1)  # no second alert for a stored posting

    def test_edited_posting_is_rewritten_automatically_without_a_new_alert(self):
        ns, db = load_publisher(MODULES[0])
        ns['publish']([make_job(1)])
        for field, value in (('description', 'Operations, now with a signing bonus'),
                             ('title', 'Senior Job 1'), ('location', 'Phoenix')):
            with self.subTest(field=field):
                edited = make_job(1)
                setattr(edited, field, value)
                totals, rows = publish_and_capture(ns, db, [edited])
                self.assertEqual(totals['written_changed'], 1)
                self.assertIn('source_payload', rows[0])
                self.assertEqual(db.jobs['Employer', URL1][field], value)
                # The same edited content on the next run is a light refresh again.
                totals, rows = publish_and_capture(ns, db, [edited])
                self.assertEqual((totals['written_changed'], totals['touched']), (0, 1))
        self.assertEqual(len(db.sends), 1)

    def test_run_bookkeeping_does_not_count_as_a_change(self):
        ns, db = load_publisher(MODULES[0])
        ns['publish']([make_job(1)])
        later = make_job(1)
        later.run_id = 'a-different-run'
        totals, _ = publish_and_capture(ns, db, [later])
        self.assertEqual((totals['written_changed'], totals['touched']), (0, 1))

    def test_postings_stored_before_hashing_are_rewritten_once_within_a_budget(self):
        ns, db = load_publisher(MODULES[0])
        jobs = [make_job(i) for i in range(5)]
        ns['publish'](jobs)
        for row in db.jobs.values():  # simulate rows stored by the previous code
            row['source_payload'].pop(ns['HASH_KEY'])
        ns['BACKFILL_PER_RUN'] = 2
        totals, _ = publish_and_capture(ns, db, jobs)
        self.assertEqual((totals['written_backfill'], totals['touched']), (2, 3))
        totals, _ = publish_and_capture(ns, db, jobs)
        self.assertEqual((totals['written_backfill'], totals['touched']), (2, 3))
        totals, _ = publish_and_capture(ns, db, jobs)
        self.assertEqual((totals['written_backfill'], totals['touched']), (1, 4))
        totals, _ = publish_and_capture(ns, db, jobs)
        self.assertEqual((totals['written_backfill'], totals['touched']), (0, 5))
        self.assertEqual(len(db.sends), 1)  # one message of five embeds, never repeated

    def test_reappearing_posting_is_reactivated_without_a_new_alert(self):
        ns, db = load_publisher(MODULES[0])
        ns['publish']([make_job(1)])
        ns['publish']([make_job(2)])  # job 1 vanished -> retired
        self.assertFalse(db.jobs['Employer', URL1]['is_active'])
        ns['publish']([make_job(1), make_job(2)])
        row = db.jobs['Employer', URL1]
        self.assertTrue(row['is_active'])
        self.assertIsNone(row['closed_at'])
        self.assertEqual(len(db.sends), 2)

    def test_already_inactive_postings_are_not_sent_for_cleanup_again(self):
        ns, db = load_publisher(MODULES[0])
        ns['publish']([make_job(1), make_job(2)])
        ns['publish']([make_job(2)])
        self.assertEqual(len(db.cleanup_requests), 1)
        ns['publish']([make_job(2)])
        ns['publish']([make_job(2)])
        self.assertEqual(len(db.cleanup_requests), 1)

    def test_full_refresh_flag_rewrites_stored_content(self):
        ns, db = load_publisher(MODULES[0])
        ns['publish']([make_job(1)])
        ns['FULL_REFRESH'] = True
        _, rows = publish_and_capture(ns, db, [make_job(1)])
        self.assertEqual(len(rows), 1)
        self.assertIn('description', rows[0])

    def test_description_is_stored_once(self):
        # source_payload must never carry description copies again (database size).
        ns, db = load_publisher(MODULES[0])
        job = make_job(1)
        job.description_raw = '<p>raw html</p>'
        job.description_clean = 'clean text'
        row = ns['serialize'](job)
        self.assertNotIn('description_raw', row['source_payload'])
        self.assertNotIn('description_clean', row['source_payload'])
        self.assertEqual(row['description'], 'Operations')
        self.assertEqual(ns['HASH_KEY'], 'content_hash_v2')
        _, rows = publish_and_capture(ns, db, [job])
        self.assertNotIn('description_raw', rows[0]['source_payload'])

    def test_rows_hashed_by_the_old_key_go_through_the_capped_backfill(self):
        ns, db = load_publisher(MODULES[0])
        jobs = [make_job(i) for i in range(3)]
        ns['publish'](jobs)
        for row in db.jobs.values():  # as stored by the code before v2
            row['source_payload']['content_hash'] = row['source_payload'].pop(ns['HASH_KEY'])
        ns['BACKFILL_PER_RUN'] = 2
        totals, rows = publish_and_capture(ns, db, jobs)
        self.assertEqual((totals['written_backfill'], totals['written_changed'], totals['touched']), (2, 0, 1))
        self.assertEqual(sum('description' in r for r in rows), 2)


if __name__ == '__main__':
    unittest.main()
