"""Reproduce the November 14 failures without contacting employer sites."""
import unittest
from unittest.mock import patch
import requests
from jobradar.providers.workday import WorkdayProvider


class Response:
    def __init__(self, status=200, data=None, malformed=False, headers=None):
        self.status_code, self.data, self.malformed = status, data, malformed
        self.headers = headers or {'Content-Type': 'text/html' if malformed else 'application/json'}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}', response=self)
    def json(self):
        if self.malformed:
            raise ValueError('not JSON')
        return self.data


def page(start=0, total=1, count=1):
    return Response(data={'total': total, 'jobPostings': [
        {'title': f'Job {i}', 'externalPath': f'/job/{i}'} for i in range(start, start + count)]})


class WorkdayRecovery(unittest.TestCase):
    @patch('jobradar.providers.workday.time.sleep')
    def test_521_then_non_json_then_success_retries_identical_page(self, sleep):
        with patch('jobradar.providers.workday.requests.post', side_effect=[Response(521), Response(malformed=True), page()]) as post:
            self.assertEqual(len(WorkdayProvider('bostondynamics').fetch('test')), 1)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        self.assertEqual(post.call_args_list[1], post.call_args_list[2])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    @patch('jobradar.providers.workday.time.sleep')
    def test_second_page_retry_keeps_first_page_and_has_no_duplicates(self, sleep):
        with patch('jobradar.providers.workday.requests.post', side_effect=[page(0, 40, 20), requests.Timeout(), page(20, 0, 20)]) as post:
            jobs = WorkdayProvider('rockwellautomation').fetch('test')
        self.assertEqual(len({j.url for j in jobs}), 40)
        self.assertEqual([c.kwargs['json']['offset'] for c in post.call_args_list], [0, 20, 20])

    @patch('jobradar.providers.workday.time.sleep')
    def test_exhaustion_raises_instead_of_returning_partial_data(self, sleep):
        with patch('jobradar.providers.workday.requests.post', side_effect=[page(0, 40, 20)] + [Response(malformed=True)] * 3):
            with self.assertRaisesRegex(RuntimeError, 'offset=20 failed after 3 attempts'):
                WorkdayProvider('autostore').fetch('test')

    @patch('jobradar.providers.workday.time.sleep')
    def test_rate_limit_wait_is_honored_and_bounded(self, sleep):
        with patch('jobradar.providers.workday.requests.post', side_effect=[Response(429, headers={'Retry-After': '7'}), page()]):
            WorkdayProvider('autostore').fetch('test')
        sleep.assert_called_once_with(7)
        with patch('jobradar.providers.workday.requests.post', return_value=Response(429, headers={'Retry-After': '120'})) as post:
            with self.assertRaisesRegex(RuntimeError, 'retry budget'):
                WorkdayProvider('autostore').fetch('test')
            self.assertEqual(post.call_count, 1)

    def test_auth_or_schema_errors_are_not_retried_or_hidden(self):
        for response, error in [(Response(403), requests.HTTPError), (Response(data={'error':'denied'}), ValueError), (Response(data=[]), ValueError)]:
            with self.subTest(response=response), patch('jobradar.providers.workday.requests.post', return_value=response) as post:
                with self.assertRaises(error): WorkdayProvider('bostondynamics').fetch('test')
                self.assertEqual(post.call_count, 1)

    def test_verified_empty_is_valid(self):
        with patch('jobradar.providers.workday.requests.post', return_value=page(total=0, count=0)):
            self.assertEqual(WorkdayProvider('bostondynamics').fetch('test'), [])
