"""Greenhouse posted date is the first-published date; the last-updated time is only a fallback."""
import types
import unittest
from unittest.mock import patch

from jobradar.providers.greenhouse import GreenhouseProvider


def board(*jobs):
    return types.SimpleNamespace(json=lambda: {'name': 'Example', 'jobs': list(jobs)})


def posting(**extra):
    return {'title': 'Engineer', 'absolute_url': 'https://job-boards.greenhouse.io/example/jobs/1',
            'location': {'name': 'Remote'}, 'content': '<p>Work</p>', **extra}


class GreenhousePostedDate(unittest.TestCase):
    def fetch(self, **extra):
        with patch('jobradar.providers.greenhouse.http_get', return_value=board(posting(**extra))):
            return GreenhouseProvider('example').fetch('test')[0]

    def test_first_published_wins_over_last_updated(self):
        job = self.fetch(first_published='2026-09-01T10:00:00-04:00', updated_at='2026-11-18T09:00:00-05:00')
        self.assertEqual(job.posted_at, '2026-09-01T10:00:00-04:00')

    def test_an_employer_edit_does_not_move_the_posted_date(self):
        before = self.fetch(first_published='2026-09-01T10:00:00-04:00', updated_at='2026-11-01T09:00:00-04:00')
        after = self.fetch(first_published='2026-09-01T10:00:00-04:00', updated_at='2026-11-18T09:00:00-05:00')
        self.assertEqual(before.posted_at, after.posted_at)

    def test_last_updated_is_used_only_when_first_published_is_missing(self):
        self.assertEqual(self.fetch(updated_at='2026-11-18T09:00:00-05:00').posted_at, '2026-11-18T09:00:00-05:00')


if __name__ == '__main__':
    unittest.main()
