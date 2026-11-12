"""Offline regression checks for review copies. HTTP is always replaced by a fake."""
import ast
import concurrent.futures
import contextlib
import copy
import datetime
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import requests

ROOT = Path(__file__).resolve().parent.parent
MODULES = ('tech',)


class Response:
    def __init__(self, data=None, status=200, text=''):
        self.data, self.status_code, self.text = data, status, text
    def json(self):
        return copy.deepcopy(self.data)
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}', response=self)


class Database:
    """In-memory fake of the exact REST surface used by the patch."""
    Request = requests.Request
    def __init__(self, module):
        self.module = module
        self.jobs = {}
        self.queue = []
        self.sends = []
        self.cleanup_requests = []
        self.write_status = 200
        self.discord_status = 200
        self.fail_send_after = None
        self.write_attempts = 0
        self.discord_attempts = 0
        self.cleanup_fail_at = None
        self.receipt_status = 200
    def get(self, url, *, params, **kwargs):
        if url.endswith('_job_notifications'):
            cursor = int(params['id'].split('.')[1])
            return Response([r for r in self.queue if r['state'] == 'pending'
                             and r['id'] > cursor][:params['limit']])
        company = params['company'][3:]
        rows = [v for (c, _), v in self.jobs.items() if c == company]
        # Mirrors the REST alias content_hash:source_payload->>content_hash.
        rows = [dict(r, content_hash=(r.get('source_payload') or {}).get('content_hash')) for r in rows]
        if 'url' in params:
            rows = [r for r in rows if r['url'] == params['url'][3:]]
        offset = params.get('offset', 0)
        return Response(rows[offset:offset + params.get('limit', 1000)])
    def post(self, url, *, json, **kwargs):
        if url == 'https://offline.invalid/discord':
            self.discord_attempts += 1
            status = self.discord_status
            if self.fail_send_after is not None and len(self.sends) >= self.fail_send_after:
                status = 429
            if status >= 400:
                return Response({'retry_after': 1}, status)
            self.sends.append(copy.deepcopy(json))
            return Response({'id': str(len(self.sends))})
        if '_job_notifications' in url:
            for row in json:
                if not any((q['company'], q['job_url']) == (row['company'], row['job_url'])
                           for q in self.queue):
                    self.queue.append(dict(copy.deepcopy(row), id=len(self.queue) + 1, state='pending'))
            return Response(None, 201)
        if url.endswith('_job_scrape_runs'):
            return Response(None, self.receipt_status)
        self.write_attempts += 1
        if self.write_status >= 400:
            return Response({}, self.write_status)
        for row in json:
            key = row['company'], row['url']
            self.jobs[key] = dict(self.jobs.get(key, {}), **row)
            self.jobs[key]['id'] = self.jobs[key].get('id', len(self.jobs))
        return Response(None, 201)
    def patch(self, url, *, params, json, **kwargs):
        if url.endswith('_job_notifications'):
            ids = [int(i) for i in params['id'][4:-1].split(',')]
            for row in self.queue:
                if row['id'] in ids and row['state'] == 'pending':
                    row.update(json)
            return Response(None, 204)
        self.cleanup_requests.append(params)
        if self.cleanup_fail_at == len(self.cleanup_requests):
            return Response({}, 400, 'Bad Request')
        company = params['company'][3:]
        urls = __import__('json').loads('[' + params['url'][4:-1] + ']')
        changed = []
        for item in urls:
            row = self.jobs.get((company, item))
            if row and row['is_active']:
                row.update(json)
                changed.append({'id': row['id']})
        return Response(changed)


def load_publisher(module):
    path = ROOT/'jobradar/publish.py'
    tree = ast.parse(path.read_text(encoding='utf8'))
    # Relative relevance imports are replaced by an explicit all-relevant fixture.
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.level)]
    ns = {'__name__': 'offline_publisher', '_is_relevant': lambda *a: True,
          '_relevance_explain': lambda *a: (True, 'test')}
    exec(compile(tree, str(path), 'exec'), ns)
    db = Database(module)
    ns.update(requests=db, DISCORD_WEBHOOK_JOBS='https://offline.invalid/discord',
              supabase_env=lambda: ('https://offline.invalid', 'fake'),
              time=types.SimpleNamespace(sleep=lambda _: None, monotonic=lambda: 0))
    return ns, db


def make_job(index):
    return types.SimpleNamespace(company='Employer', title=f'Job {index}',
        url=f'https://careers.example/jobs/{index}', location='Tempe',
        description='Operations', description_raw='', description_clean='',
        source='test', provider_slug='employer', run_id='test', salary=None,
        remote=False, posted_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        is_active=True)


class RepairTests(unittest.TestCase):
    def test_cleanup_bounds_and_exact_counts(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                urls = {f'https://careers.example/jobs/{i}/' + 'long-title-' * 12 for i in range(1400)}
                for i, url in enumerate(urls):
                    db.jobs['Employer', url] = {'id': i, 'url': url, 'is_active': True}
                self.assertEqual(ns['deactivate']('https://offline.invalid', 'fake', 'Employer', urls), 1400)
                self.assertGreater(len(db.cleanup_requests), 1)
                for params in db.cleanup_requests:
                    request = requests.Request('PATCH', ns['_rest']('https://offline.invalid', ns['TABLE_JOBS']), params=params).prepare()
                    self.assertLessEqual(len(request.url.encode()), 6000)
                self.assertFalse(any(r['is_active'] for r in db.jobs.values()))

    def test_cleanup_special_characters_and_no_false_counts(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                url = 'https://careers.example/a,(b).html?q="x"'
                db.jobs['Employer', url] = {'id': 1, 'url': url, 'is_active': True}
                self.assertEqual(ns['deactivate']('https://offline.invalid', 'fake', 'Employer', {url, 'https://missing.example'}), 1)

    def test_cleanup_failure_raises(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                db.cleanup_fail_at = 1
                with self.assertRaises(requests.HTTPError):
                    ns['deactivate']('https://offline.invalid', 'fake', 'Employer', {'https://example/1'})

    def test_exhausted_database_retry_raises(self):
        for module in MODULES:
            for status in (429, 500, 503):
                with self.subTest(module=module, status=status):
                    ns, db = load_publisher(module)
                    db.write_status = status
                    with self.assertRaises(requests.HTTPError):
                        ns['upsert']('https://offline.invalid', 'fake', [{'company': 'Employer', 'url': 'https://example/1'}])
                    self.assertEqual(db.write_attempts, 3)


    def test_transport_retry_reuses_exact_upsert_batch(self):
        for error in (requests.ReadTimeout, requests.ConnectionError):
            ns, db = load_publisher(MODULES[0])
            rows = [{'company': 'Employer', 'url': 'https://example/1'}]
            with patch.object(db, 'post', side_effect=[error('network interrupted'), Response(None, 201)]) as post:
                ns['upsert']('https://offline.invalid', 'fake', rows)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args_list[0], post.call_args_list[1])

    def test_exhausted_transport_retry_preserves_pending_intent(self):
        ns, db = load_publisher(MODULES[0])
        original = db.post
        writes = []
        def interrupted(url, **kwargs):
            if '_job_notifications' not in url:
                writes.append(url)
                raise requests.ReadTimeout('database response lost')
            return original(url, **kwargs)
        with patch.object(db, 'post', side_effect=interrupted):
            with self.assertRaises(requests.ReadTimeout):
                ns['publish']([make_job(1)])
        self.assertEqual(len(writes), 3)
        self.assertEqual(db.queue[0]['state'], 'pending')
        self.assertEqual(db.sends, [])
        ns['publish']([make_job(1)])
        self.assertEqual(len(db.sends), 1)

    def test_committed_upsert_with_lost_response_does_not_duplicate(self):
        ns, db = load_publisher(MODULES[0])
        original = db.post
        lost = []
        def commit_then_disconnect(url, **kwargs):
            response = original(url, **kwargs)
            if '_jobs?on_conflict=' in url and not lost:
                lost.append(True)
                raise requests.ConnectionError('response lost after commit')
            return response
        with patch.object(db, 'post', side_effect=commit_then_disconnect):
            ns['publish']([make_job(1)])
        self.assertEqual(db.write_attempts, 2)
        self.assertEqual(len(db.jobs), 1)
        self.assertEqual(len(db.sends), 1)
        self.assertEqual(db.queue[0]['state'], 'delivered')

    def test_nonretryable_database_error_fails_immediately(self):
        ns, db = load_publisher(MODULES[0])
        db.write_status = 400
        with self.assertRaises(requests.HTTPError):
            ns['upsert']('https://offline.invalid', 'fake', [{'company': 'Employer', 'url': 'https://example/1'}])
        self.assertEqual(db.write_attempts, 1)

    def test_failed_write_preserves_intent_and_next_run_delivers_once(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                db.write_status = 503
                with self.assertRaises(requests.HTTPError):
                    ns['publish']([make_job(1)])
                self.assertEqual(len(db.queue), 1)
                self.assertEqual(db.queue[0]['state'], 'pending')
                self.assertEqual(len(db.sends), 0)
                # Queue drain cannot announce an intent whose job was never saved.
                self.assertEqual(ns['notify_new_jobs']([]), 0)
                db.write_status = 200
                ns['publish']([make_job(1)])
                ns['publish']([make_job(1)])
                self.assertEqual(len(db.sends), 1)
                self.assertEqual(db.queue[0]['state'], 'delivered')

    def test_partial_discord_failure_retries_only_pending_chunk(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                db.fail_send_after = 1
                jobs = [make_job(i) for i in range(10)]
                with self.assertRaises(requests.HTTPError):
                    ns['publish'](jobs)
                self.assertEqual(sum(r['state'] == 'delivered' for r in db.queue), 5)
                self.assertEqual(sum(r['state'] == 'pending' for r in db.queue), 5)
                db.fail_send_after = None
                ns['publish'](jobs)
                self.assertEqual(len(db.sends), 2)
                self.assertEqual(sum(r['state'] == 'delivered' for r in db.queue), 10)

    def test_existing_rows_are_not_backfilled_as_new_alerts(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                job = make_job(1)
                db.jobs[job.company, job.url] = dict(ns['serialize'](job), id=1)
                ns['publish']([job])
                self.assertEqual(db.queue, [])
                self.assertEqual(db.sends, [])

    def test_partial_observation_does_not_retire_existing_job(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                old = make_job(1)
                db.jobs[old.company, old.url] = dict(ns['serialize'](old), id=1)
                ns['publish']([make_job(2)], allow_deactivate=False)
                self.assertTrue(db.jobs[old.company, old.url]['is_active'])
                self.assertEqual(db.cleanup_requests, [])

    def test_receipt_http_error_is_exposed(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                db.receipt_status = 403
                with self.assertRaises(requests.HTTPError):
                    ns['record_run']('https://offline.invalid', 'fake', 'source', 'success', 1)

    def test_discord_retry_seconds_and_budget(self):
        for module in MODULES:
            with self.subTest(module=module):
                ns, db = load_publisher(module)
                self.assertEqual(ns['_discord_retry_after'](Response({'retry_after': 90})), 90)
                with self.assertRaises(RuntimeError):
                    ns['_discord_retry_after'](Response({'retry_after': 121}))
                self.assertEqual(ns['DISCORD_PACE_SECONDS'], 2.1)



if __name__ == '__main__':
    # Suppress fake HTTP progress; keep unittest assertions and failures visible.
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main(verbosity=2)
