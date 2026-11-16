import copy
import unittest
from unittest.mock import Mock, patch

import requests

from monitor.notify import deliver, payload_for, prepare_health_summary, queue_health_event
from monitor.rules import evaluate, role_of
from monitor.run import read_config
from monitor.store import fresh_store
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_, PREFS = read_config(ROOT)
NOW = '2026-11-16T06:00:00+00:00'
WEBHOOK = {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/123/test_token'}


def health(state, source_id, status='error', error='HTTP 504', at=NOW):
    previous = copy.deepcopy(state['sources'].get(source_id, {}))
    current = {'id': source_id, 'status': status, 'severity': 'ok' if status == 'ok' else 'error',
               'capability': 'full', 'errors': [] if status == 'ok' else [error], 'last_attempt': at}
    state['sources'][source_id] = current
    queue_health_event(state, {'id': source_id, 'institution': 'School ' + source_id}, current, previous, at)
    return current


class NotificationReviewTests(unittest.TestCase):
    def test_adjunct_is_teaching_specific_and_uses_existing_role_rules(self):
        for title in ('Adjunct Faculty (Philosophy)', 'Adjunct Lecturer in Ethics', 'Adjunct Teaching Faculty'):
            self.assertEqual(role_of(title), 'lecturer')
            job = {'title': title, 'department': 'Humanities', 'description': 'Teaching philosophy and ethics.',
                   'source_id': 'test', 'employment_type': 'part-time'}
            self.assertTrue(evaluate(job, PREFS)['matches'])
        for title in ('Adjunct Research Fellow', 'Adjunct Professor of Research', 'Adjunct Advisory Member'):
            self.assertEqual(role_of(title), 'other')
        self.assertTrue(PREFS['notifications']['require_part_time'])

    def test_health_only_queues_transitions_and_recovery(self):
        state = fresh_store()
        health(state, 'one', status='ok')
        self.assertFalse(state['outbox'])
        health(state, 'one')
        first_count = len(state['outbox'])
        health(state, 'one', at='2026-11-17T06:00:00+00:00')
        self.assertEqual(len(state['outbox']), first_count)
        health(state, 'one', status='ok', at='2026-11-17T07:00:00+00:00')
        self.assertEqual(len(state['outbox']), first_count + 1)
        self.assertEqual(list(state['outbox'].values())[-1]['change'], 'recovered')

    @patch.dict('os.environ', WEBHOOK)
    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_eleven_failures_become_one_daily_digest_and_ack_together(self, post, sleep):
        state = fresh_store()
        for number in range(11):
            health(state, str(number))
        post.return_value = Mock(status_code=200, json=lambda: {'id': 'ack-summary'})
        saved = []
        self.assertEqual(deliver(state, NOW, lambda: saved.append(copy.deepcopy(state))), 1)
        self.assertEqual(post.call_count, 1)
        self.assertIn('未完整來源 11 個', post.call_args.kwargs['json']['content'])
        self.assertTrue(all(event['state'] == 'sent' for event in state['outbox'].values()))
        self.assertEqual(deliver(state, NOW, lambda: None), 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(deliver(state, '2026-11-17T06:00:00+00:00', lambda: None), 1)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(saved)

    @patch.dict('os.environ', WEBHOOK)
    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_recovery_after_daily_digest_emits_only_state_change(self, post, sleep):
        state = fresh_store()
        health(state, 'one'); health(state, 'two')
        post.return_value = Mock(status_code=200, json=lambda: {'id': 'ack'})
        self.assertEqual(deliver(state, NOW, lambda: None), 1)
        recovered_at = '2026-11-16T07:00:00+00:00'
        health(state, 'one', status='ok', at=recovered_at)
        self.assertEqual(deliver(state, recovered_at, lambda: None), 1)
        content = post.call_args.kwargs['json']['content']
        self.assertIn('恢復完整 1 個', content)
        self.assertIn('School one', content)
        self.assertNotIn('School two', content)
        self.assertEqual(deliver(state, recovered_at, lambda: None), 0)

    @patch.dict('os.environ', WEBHOOK)
    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_failed_summary_ack_keeps_transitions_pending_for_retry(self, post, sleep):
        state = fresh_store(); health(state, 'one')
        post.side_effect = requests.Timeout()
        self.assertEqual(deliver(state, NOW, lambda: None), 0)
        self.assertTrue(all(event['state'] == 'pending' for event in state['outbox'].values()))
        post.side_effect = None
        post.return_value = Mock(status_code=200, json=lambda: {'id': 'confirmed'})
        self.assertEqual(deliver(state, NOW, lambda: None), 1)
        self.assertTrue(all(event['state'] == 'sent' for event in state['outbox'].values()))

    @patch.dict('os.environ', WEBHOOK)
    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_legacy_backlog_is_coalesced_and_resolved_outage_not_sent(self, post, sleep):
        state = fresh_store()
        state['sources'] = {'one': {'status': 'partial'}, 'two': {'status': 'ok'}}
        for number, source_id in enumerate(['one', 'one', 'two']):
            key = 'health:old:' + str(number)
            state['outbox'][key] = {'id': key, 'kind': 'health', 'source_id': source_id, 'state': 'pending',
                                   'created_at': f'2026-11-1{number}T00:00:00+00:00', 'errors': [f'failure {number}']}
        post.return_value = Mock(status_code=200, json=lambda: {'id': 'ack'})
        self.assertEqual(deliver(state, NOW, lambda: None), 1)
        content = post.call_args.kwargs['json']['content']
        self.assertIn('failure 1', content)
        self.assertNotIn('failure 0', content)
        self.assertNotIn('failure 2', content)
        self.assertEqual(state['outbox']['health:old:0']['state'], 'superseded')
        self.assertEqual(state['outbox']['health:old:2']['state'], 'resolved')

    @patch.dict('os.environ', WEBHOOK)
    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_closed_posts_are_suppressed_and_incomplete_posts_deferred(self, post, sleep):
        state = fresh_store()
        for key, status, complete in [('closed', 'closed', True), ('incomplete', 'open', False)]:
            state['jobs'][key] = {'status': status, 'matches': True, 'detail_complete': complete}
            state['outbox'][key] = {'id': key, 'kind': 'new', 'job_id': key, 'state': 'pending', 'created_at': NOW}
        self.assertEqual(deliver(state, NOW, lambda: None), 0)
        post.assert_not_called()
        self.assertEqual(state['outbox']['closed']['state'], 'suppressed')
        self.assertEqual(state['outbox']['incomplete']['state'], 'pending')

    def test_digest_uses_hong_kong_date_and_never_mentions_discord_users(self):
        state = fresh_store(); health(state, '@everyone')
        prepare_health_summary(state, '2026-11-16T16:05:00+00:00')
        event = state['outbox']['health-digest:2026-11-17']
        payload = payload_for(event, state)
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertLessEqual(len(payload['content']), 1900)


if __name__ == '__main__':
    unittest.main()
