"""After Discord accepts a message, recording it must survive one failed database write.

If the record is not written, the next run sends the same jobs again (Codex review T04, 2026-11-19).
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

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class SendIsRecorded(unittest.TestCase):
    def test_one_server_error_does_not_lose_the_record(self):
        with mock.patch.object(publish.requests, "patch", side_effect=[Reply(503), Reply(204)]) as patch, \
                mock.patch.object(publish.time, "sleep"):
            publish._ack_notifications("https://db.example", "key", [1, 2], "delivered", "123")
        self.assertEqual(patch.call_count, 2)
        self.assertEqual(patch.call_args.kwargs["params"]["state"], "eq.pending")

    def test_one_dropped_connection_does_not_lose_the_record(self):
        with mock.patch.object(publish.requests, "patch", side_effect=[requests.ConnectionError("reset"), Reply(204)]) as patch, \
                mock.patch.object(publish.time, "sleep"):
            publish._ack_notifications("https://db.example", "key", [1], "delivered", "123")
        self.assertEqual(patch.call_count, 2)

    def test_three_failures_fail_the_run(self):
        with mock.patch.object(publish.requests, "patch", side_effect=[Reply(500), Reply(500), Reply(500)]), \
                mock.patch.object(publish.time, "sleep"):
            with self.assertRaises(requests.HTTPError):
                publish._ack_notifications("https://db.example", "key", [1], "delivered", "123")


if __name__ == "__main__":
    unittest.main()
