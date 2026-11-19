"""One HTTP 500 on a database read must not end the run.

Run 44496422818 (2026-11-19) stored every job, queued 11 for Discord, then failed on one read that answered
HTTP 500 while the table was being rewritten. The queued jobs were not sent in that run.
"""
import os
import unittest
from unittest import mock

import requests

os.environ.setdefault("JOBRADAR_MODULE", "robotics")

from jobradar import publish


class Reply:
    def __init__(self, status):
        self.status_code = status


class DatabaseReadRetry(unittest.TestCase):
    def test_server_error_is_read_again(self):
        with mock.patch.object(publish.requests, "get", side_effect=[Reply(500), Reply(200)]) as get, \
                mock.patch.object(publish.time, "sleep") as sleep:
            self.assertEqual(publish._get("https://db.example/rest/v1/x").status_code, 200)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()

    def test_dropped_connection_is_read_again(self):
        with mock.patch.object(publish.requests, "get", side_effect=[requests.ConnectionError("reset"), Reply(200)]), \
                mock.patch.object(publish.time, "sleep"):
            self.assertEqual(publish._get("https://db.example/rest/v1/x").status_code, 200)

    def test_three_server_errors_are_returned_so_the_caller_fails(self):
        with mock.patch.object(publish.requests, "get", side_effect=[Reply(500), Reply(502), Reply(503)]) as get, \
                mock.patch.object(publish.time, "sleep"):
            self.assertEqual(publish._get("https://db.example/rest/v1/x").status_code, 503)
        self.assertEqual(get.call_count, 3)

    def test_client_error_is_not_repeated(self):
        with mock.patch.object(publish.requests, "get", side_effect=[Reply(400)]) as get:
            self.assertEqual(publish._get("https://db.example/rest/v1/x").status_code, 400)
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
