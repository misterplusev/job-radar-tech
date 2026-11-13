"""The common Discord sender, used for a module other than robotics. HTTP is always a fake.

Sportsbook jobs are queued by their own scraper (which cannot reach Discord) and delivered
here by `run.py deliver` with the same code path that delivers robotics jobs.
"""
import datetime
import os
import unittest
from unittest.mock import patch

from test_reliability import Response, load_publisher

NOW = datetime.datetime.now(datetime.timezone.utc)


def load(module):
    ns, db = load_publisher(module)
    original = db.post

    def post(url, *, json, **kwargs):
        response = original(url, json=json, **kwargs)
        if url == 'https://offline.invalid/discord' and response.status_code < 400:
            return Response({'id': f'msg-{len(db.sends)}', 'channel_id': '1478835376201269402',
                             'webhook_id': '1478835425303859301'})
        return response
    db.post = post
    return ns, db


def queued(db, index, *, posted_at=None, saved=True, active=True, logo=True):
    """A row as the sportsbook scraper queues it, plus the saved job it refers to."""
    url = f'https://careers.example/sportsbook/{index}'
    payload = {'company': 'DraftKings', 'title': f'Trader {index}', 'url': url, 'location': 'Boston, MA',
               'description': 'Sportsbook trading role', 'ats': 'workday',
               'posted_at': posted_at or NOW.isoformat(), 'scraped_at': NOW.isoformat()}
    if logo:
        payload['logo_url'] = 'https://sportsbook-jobs-dashboard.vercel.app/logos/draftkings_com.png'
    db.queue.append({'id': len(db.queue) + 1, 'company': 'DraftKings', 'job_url': url, 'payload': payload,
                     'state': 'pending'})
    if saved:
        db.jobs[('DraftKings', url)] = {'id': index, 'company': 'DraftKings', 'url': url, 'is_active': active}
    return url


class ModuleSelection(unittest.TestCase):
    def test_robotics_is_the_default_and_keeps_its_tables(self):
        ns, _ = load_publisher('robotics')
        self.assertEqual(ns['MODULE_NAME'], 'robotics')
        self.assertEqual((ns['TABLE_JOBS'], ns['TABLE_NOTIFICATIONS']), ('robotics_jobs', 'robotics_job_notifications'))

    def test_sportsbook_uses_its_own_tables(self):
        ns, _ = load('sportsbook')
        self.assertEqual(ns['MODULE_NAME'], 'sportsbook')
        self.assertEqual((ns['TABLE_JOBS'], ns['TABLE_NOTIFICATIONS']), ('jobs', 'sportsbook_job_notifications'))

    def test_an_unknown_module_refuses_to_start(self):
        with self.assertRaises(RuntimeError):
            load('casino')


class SportsbookDelivery(unittest.TestCase):
    def test_queued_jobs_are_sent_once_and_recorded_with_discord_ids(self):
        ns, db = load('sportsbook')
        for i in (1, 2):
            queued(db, i)
        self.assertEqual(ns['notify_new_jobs']([]), 2)
        self.assertEqual(len(db.sends), 1)
        message = db.sends[0]
        self.assertEqual(message['content'], 'New sportsbook jobs discovered: 2')
        embed = message['embeds'][0]
        self.assertEqual(embed['footer']['text'], 'ABET Sportsbook Jobs Monitor')
        self.assertEqual(embed['color'], 0x38BDF8)
        self.assertEqual([f['name'] for f in embed['fields']], ['Source', 'Company', 'Location', 'Posted'])
        self.assertEqual(embed['thumbnail']['url'], 'https://sportsbook-jobs-dashboard.vercel.app/logos/draftkings_com.png')
        for row in db.queue:
            self.assertEqual(row['state'], 'delivered')
            self.assertEqual(row['discord_message_id'], 'msg-1')
            self.assertEqual(row['discord_channel_id'], '1478835376201269402')
            self.assertEqual(row['discord_webhook_id'], '1478835425303859301')
            self.assertTrue(row['delivered_at'])
        # A second run finds nothing pending and sends nothing.
        self.assertEqual(ns['notify_new_jobs']([]), 0)
        self.assertEqual(len(db.sends), 1)

    def test_five_jobs_per_message(self):
        ns, db = load('sportsbook')
        for i in range(1, 8):
            queued(db, i)
        self.assertEqual(ns['notify_new_jobs']([]), 7)
        self.assertEqual([len(m['embeds']) for m in db.sends], [5, 2])
        self.assertEqual({r['discord_message_id'] for r in db.queue}, {'msg-1', 'msg-2'})

    def test_a_job_that_is_not_saved_yet_stays_pending_and_is_not_announced(self):
        ns, db = load('sportsbook')
        queued(db, 1, saved=False)
        self.assertEqual(ns['notify_new_jobs']([]), 0)
        self.assertEqual((db.queue[0]['state'], len(db.sends)), ('pending', 0))

    def test_a_job_closed_before_sending_is_closed_out_without_a_message(self):
        ns, db = load('sportsbook')
        queued(db, 1, active=False)
        self.assertEqual(ns['notify_new_jobs']([]), 0)
        self.assertEqual([r['state'] for r in db.queue], ['inactive'])
        self.assertEqual(len(db.sends), 0)

    def test_sportsbook_jobs_are_never_dropped_for_their_posting_date(self):
        """The sender this replaces has no posting-age filter; neither does this one."""
        ns, db = load('sportsbook')
        queued(db, 1, posted_at=(NOW - datetime.timedelta(days=40)).isoformat())
        queued(db, 2, posted_at=(NOW - datetime.timedelta(days=400)).date().isoformat())
        self.assertEqual(ns['notify_new_jobs']([]), 2)
        self.assertEqual([r['state'] for r in db.queue], ['delivered', 'delivered'])

    def test_robotics_still_expires_postings_older_than_its_limit(self):
        ns, db = load_publisher('robotics')
        old = (NOW - datetime.timedelta(days=40)).isoformat()
        db.queue.append({'id': 1, 'company': 'Employer', 'job_url': 'https://careers.example/jobs/1', 'state': 'pending',
                         'payload': {'company': 'Employer', 'title': 'T', 'url': 'https://careers.example/jobs/1', 'posted_at': old}})
        db.jobs[('Employer', 'https://careers.example/jobs/1')] = {'id': 1, 'company': 'Employer',
                                                                  'url': 'https://careers.example/jobs/1', 'is_active': True}
        self.assertEqual(ns['notify_new_jobs']([]), 0)
        self.assertEqual(db.queue[0]['state'], 'expired')

    def test_a_discord_failure_leaves_every_row_pending(self):
        ns, db = load('sportsbook')
        queued(db, 1)
        db.discord_status = 500
        with self.assertRaises(Exception):
            ns['notify_new_jobs']([])
        self.assertEqual(db.queue[0]['state'], 'pending')
        self.assertNotIn('discord_message_id', db.queue[0])

    def test_pending_rows_without_a_webhook_fail_loudly(self):
        ns, db = load('sportsbook')
        queued(db, 1)
        ns['DISCORD_WEBHOOK_JOBS'] = ''
        with self.assertRaises(RuntimeError):
            ns['notify_new_jobs']([])
        self.assertEqual(db.queue[0]['state'], 'pending')

    def test_date_only_posting_dates_become_valid_discord_timestamps(self):
        ns, db = load('sportsbook')
        today = NOW.date().isoformat()
        queued(db, 1, posted_at=today)
        self.assertEqual(ns['notify_new_jobs']([]), 1)
        embed = db.sends[0]['embeds'][0]
        self.assertEqual(embed['timestamp'], today + 'T00:00:00+00:00')
        self.assertEqual(embed['fields'][-1], {'name': 'Posted', 'value': today, 'inline': True})

    def test_no_logo_in_the_payload_means_no_thumbnail(self):
        ns, db = load('sportsbook')
        queued(db, 1, logo=False)
        ns['notify_new_jobs']([])
        self.assertNotIn('thumbnail', db.sends[0]['embeds'][0])


class TechUsesTheSameFile(unittest.TestCase):
    def test_tech_settings(self):
        ns, _ = load('tech')
        self.assertEqual((ns['TABLE_JOBS'], ns['TABLE_NOTIFICATIONS'], ns['TABLE_RUNS']),
                         ('tech_jobs', 'tech_job_notifications', 'tech_job_scrape_runs'))
        self.assertFalse(ns['RELEVANCE_ON'])
        embed = ns['_embed']({'company': 'Stripe Inc', 'title': None, 'url': 'https://x/1', 'posted_at': '2026-11-12'})
        self.assertEqual(embed['title'], 'New tech job posting')
        self.assertEqual(embed['footer']['text'], 'ABET Tech Jobs Monitor')
        self.assertEqual(embed['thumbnail']['url'], 'https://misterplusev.github.io/jobradar-tech-board/logos/stripe-inc.png')

    def test_tech_delivery_wording_and_ledger(self):
        ns, db = load('tech')
        queued(db, 1)
        self.assertEqual(ns['notify_new_jobs']([]), 1)
        self.assertEqual(db.sends[0]['content'], 'New tech jobs discovered: 1')
        self.assertEqual(db.queue[0]['state'], 'delivered')
        self.assertNotIn('discord_channel_id', db.queue[0])


class OneSendFunction(unittest.TestCase):
    def test_every_module_sends_through_send_discord_message(self):
        for module in ('robotics', 'tech', 'sportsbook'):
            ns, db = load(module)
            calls = []
            real = ns['send_discord_message']
            ns['send_discord_message'] = lambda webhook, payload, **k: calls.append(webhook) or real(webhook, payload, **k)
            queued(db, 1)
            ns['notify_new_jobs']([])
            self.assertEqual(calls, ['https://offline.invalid/discord'], module)

    def test_a_message_without_an_id_is_never_a_delivery(self):
        ns, db = load('robotics')
        db.post = lambda url, **k: Response({})
        with self.assertRaises(RuntimeError):
            ns['send_discord_message']('https://offline.invalid/discord', {'content': 'x'})


class RoboticsUnchanged(unittest.TestCase):
    def test_robotics_message_format_is_what_it_was(self):
        ns, db = load_publisher('robotics')
        job = {'company': 'Employer', 'title': 'Robot Technician', 'url': 'https://careers.example/jobs/9',
               'location': 'Tempe', 'description': 'Operations', 'ats': 'greenhouse', 'posted_at': '2026-11-12'}
        embed = ns['_embed'](job)
        self.assertEqual(embed['footer']['text'], 'ABET Robotics Jobs Monitor')
        self.assertEqual(embed['color'], 0x34D399)
        self.assertEqual(embed['timestamp'], '2026-11-12')
        self.assertEqual([f['name'] for f in embed['fields']], ['Source', 'Company', 'Location'])
        self.assertEqual(embed['thumbnail']['url'], 'https://misterplusev.github.io/robotics-jobradar-board/logos/employer.png')


if __name__ == '__main__':
    unittest.main()
