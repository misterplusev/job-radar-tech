import contextlib
import datetime
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from jobradar.providers.greenhouse import GreenhouseProvider
from jobradar.providers.lever import LeverProvider
from jobradar.providers.ashby import AshbyProvider
from jobradar.providers.amazon import AmazonProvider
from jobradar.providers.workday import WorkdayProvider
from jobradar.fetch_worker import fetch_provider
from jobradar import publish
import run

def response(data):
    return types.SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)

class CompletenessTests(unittest.TestCase):
    def test_http_errors_never_become_successful_empty_boards(self):
        for name, provider in [('greenhouse', GreenhouseProvider('x')), ('lever', LeverProvider('x')), ('ashby', AshbyProvider('x'))]:
            with self.subTest(provider=name), patch(f'jobradar.providers.{name}.http_get', side_effect=RuntimeError('HTTP failed')):
                with self.assertRaises(RuntimeError): provider.fetch('test')

    def test_invalid_board_payload_is_not_empty(self):
        with patch('jobradar.providers.greenhouse.http_get', return_value=response({'error': 'denied'})):
            with self.assertRaises(ValueError): GreenhouseProvider('x').fetch('test')

    def test_amazon_page_failure_is_not_partial_success(self):
        first = {'hits': 200, 'jobs': [{'job_id': '1', 'title': 'Operations', 'job_path': '/jobs/1'}]}
        with patch('jobradar.providers.amazon.http_get', side_effect=[response(first), RuntimeError('page 2 failed')]), patch('jobradar.providers.amazon.time.sleep'):
            with self.assertRaises(RuntimeError): AmazonProvider('robotics').fetch('test')

    def test_workday_page_failure_is_not_partial_success(self):
        first = {'total': 40, 'jobPostings': [{'title': 'Operations', 'externalPath': '/job/1'}]}
        with patch('jobradar.providers.workday.requests.post', side_effect=[response(first), RuntimeError('page 2 failed')]):
            with self.assertRaises(RuntimeError): WorkdayProvider('bostondynamics').fetch('test')

    def test_provider_process_has_a_real_deadline(self):
        with patch('jobradar.fetch_worker.subprocess.run', side_effect=subprocess.TimeoutExpired('provider', 1)) as child:
            with self.assertRaises(subprocess.TimeoutExpired): fetch_provider(GreenhouseProvider('x'), 'test', timeout=1)
            self.assertEqual(child.call_args.kwargs['timeout'], 1)

    def test_workday_retains_first_page_total_when_later_totals_are_zero(self):
        pages = [response({'total': 60 if page == 0 else 0,
            'jobPostings': [{'title': f'Job {page * 20 + i}', 'externalPath': f'/job/{page * 20 + i}'}
                            for i in range(20)]}) for page in range(3)]
        with patch('jobradar.providers.workday.requests.post', side_effect=pages) as http:
            self.assertEqual(len(WorkdayProvider('bostondynamics').fetch('test')), 60)
            self.assertEqual(http.call_count, 3)

    def test_partial_run_writes_degraded_manifest_and_preserves_existing_jobs(self):
        good = types.SimpleNamespace(source='greenhouse', provider_slug='good')
        bad = types.SimpleNamespace(source='greenhouse', provider_slug='bad')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(run, 'Path', side_effect=lambda value: root/value), patch.object(run, 'build_providers', return_value=[good, bad]), patch.object(publish, 'supabase_env', return_value=('offline', 'fake')), patch.object(publish, 'record_run'), patch.object(publish, 'publish', return_value={}) as writer, patch('jobradar.fetch_worker.fetch_provider', side_effect=[[object()], RuntimeError('provider failed')]), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'Partial scrape'): run._ingest_supabase()
            self.assertFalse(writer.call_args.kwargs['allow_deactivate'])
            manifest = json.loads((root/'data/runtime/latest_manifest.json').read_text())
            self.assertEqual(manifest['status'], 'degraded')
            self.assertEqual([r['complete'] for r in manifest['providers']], [True, False])

    def test_verified_empty_source_is_passed_for_reconciliation(self):
        provider = types.SimpleNamespace(source='greenhouse', provider_slug='empty')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(run, 'Path', side_effect=lambda value: root/value), patch.object(run, 'build_providers', return_value=[provider]), patch.object(publish, 'supabase_env', return_value=('offline', 'fake')), patch.object(publish, 'record_run'), patch.object(publish, 'publish', return_value={}) as writer, patch('jobradar.fetch_worker.fetch_provider', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run._ingest_supabase(), 0)
            self.assertEqual(writer.call_args.kwargs['empty_providers'], [('greenhouse', 'empty')])
            self.assertTrue(writer.call_args.kwargs['allow_deactivate'])

if __name__ == '__main__': unittest.main()
