import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.adapters import Batch, collect, parse_uow
from monitor.http import CrawlError, PublicClient, RobotsPolicy
from monitor.model import record, parse_date, deadline_from
from monitor.notify import deliver, payload_for
from monitor.official import local_date, parse_hkbu_page
from monitor.rules import evaluate
from monitor.schedule import schedule_description
from monitor.run import read_config
from monitor.store import fresh_store, reconcile, expire_jobs, export, load_store, atomic_json

ROOT = Path(__file__).resolve().parents[1]
SOURCES, PREFS = read_config(ROOT)
SOURCE = {'id': 'test', 'group': 'test', 'institution': 'Test University', 'name': '測試', 'url': 'https://example.edu/jobs', 'allowed_hosts': ['example.edu'], 'enabled': True, 'notes': ''}
DAY = '2026-09-05T00:17:00+00:00'


def vacancy(reference='A', **fields):
    return record(SOURCE, 'Part-time Lecturer in Philosophy', 'https://example.edu/jobs/' + reference, reference,
                  description='Teach philosophy and critical thinking skills to undergraduate students.', employment_type='part-time', **fields)


class MatchingTests(unittest.TestCase):
    def test_teaching_skills_are_subject_evidence(self):
        job = vacancy()
        result = evaluate(job, PREFS)
        self.assertTrue(result['matches'])
        self.assertIn('critical-thinking', [s['id'] for s in result['subjects']])

    def test_generic_phd_and_equal_opportunity_are_not_subjects(self):
        job = vacancy()
        job.update(title='Part-time Lecturer in Nursing', description='Applicants should possess a Doctor of Philosophy or Master of Philosophy.\nThe University is committed to equality, ethics and diversity.\nApplicants should have strong critical thinking skills.')
        self.assertFalse(evaluate(job, PREFS)['matches'])

    def test_bare_ai_and_fulltime_do_not_notify(self):
        job = vacancy()
        job.update(title='Part-time Lecturer in AI', description='Teach machine learning, AI and deep learning.')
        self.assertFalse(evaluate(job, PREFS)['matches'])
        job.update(title='Lecturer in AI literacy', employment_type='full-time')
        self.assertFalse(evaluate(job, PREFS)['matches'])

    def test_institution_exclusion_is_separate(self):
        prefs = copy.deepcopy(PREFS)
        prefs['notifications']['institutions'] = ['another']
        self.assertFalse(evaluate(vacancy(), prefs)['matches'])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_store()
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy()]), DAY, PREFS)

    def test_silent_baseline_and_repeat_and_new(self):
        self.assertFalse(self.state['outbox'])
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy(), vacancy('B')]), DAY, PREFS)
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy(), vacancy('B')]), DAY, PREFS)
        self.assertEqual(len(self.state['jobs']), 2)
        self.assertEqual(list(self.state['outbox']), ['new:' + vacancy('B')['id']])
        self.assertEqual(sum(e['kind'] == 'new' for e in self.state['history']), 1)

    def test_existing_partial_dataset_is_not_rebaselined(self):
        self.state['sources']['test'].pop('baseline_started', None)
        self.state['sources']['test'].pop('baseline_completed', None)
        self.state['sources']['test']['status'] = 'partial'
        result = reconcile(self.state, SOURCE, Batch(jobs=[vacancy(), vacancy('B')]), DAY, PREFS)
        self.assertFalse(result['baseline'])
        self.assertIn('new:' + vacancy('B')['id'], self.state['outbox'])

    def test_failed_crawl_never_means_missing(self):
        for _ in range(5):
            reconcile(self.state, SOURCE, Batch(complete=False, errors=['timeout']), DAY, PREFS)
        job = self.state['jobs'][vacancy()['id']]
        self.assertEqual(job['status'], 'open')
        self.assertEqual(job['missing_count'], 0)
        self.assertEqual(self.state['sources']['test']['status'], 'error')

    def test_missing_requires_three_complete_confirmations(self):
        for number in range(3):
            reconcile(self.state, SOURCE, Batch(jobs=[], explicit_empty=True), DAY, PREFS)
            self.assertEqual(self.state['jobs'][vacancy()['id']]['status'], 'missing' if number == 2 else 'open')

    def test_count_collapse_does_not_remove_jobs(self):
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy(str(i)) for i in range(10)]), DAY, PREFS)
        result = reconcile(self.state, SOURCE, Batch(jobs=[vacancy('0')]), DAY, PREFS)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.state['jobs'][vacancy('1')['id']]['missing_count'], 0)

    def test_partial_details_preserve_verified_dates_and_status(self):
        old = vacancy(posted_date='2026-08-20', deadline='2026-09-01', deadline_type='closing', deadline_raw='Closing date: 1 September 2026')
        reconcile(self.state, SOURCE, Batch(jobs=[old]), DAY, PREFS)
        for kind in ('unknown', 'screening-or-closing'):
            incoming = vacancy(detail_complete=False, deadline_type=kind)
            reconcile(self.state, SOURCE, Batch(jobs=[incoming], complete=False), DAY, PREFS)
            actual = self.state['jobs'][old['id']]
            self.assertEqual(actual['posted_date'], '2026-08-20')
            self.assertEqual(actual['deadline'], '2026-09-01')
            self.assertEqual(actual['status'], 'closed')

    def test_expiry_without_successful_fetch_and_review_is_not_deadline(self):
        closing = vacancy('B', deadline='2026-09-05', deadline_type='closing')
        review = vacancy('C', deadline='2026-09-05', deadline_type='review')
        reconcile(self.state, SOURCE, Batch(jobs=[closing, review]), DAY, PREFS)
        expire_jobs(self.state, '2026-09-05T16:01:00+00:00', PREFS)
        self.assertEqual(self.state['jobs'][closing['id']]['status'], 'closed')
        self.assertEqual(self.state['jobs'][review['id']]['status'], 'open')

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/123/test_token'})
    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_pending_alert_survives_partial_details_then_sends_once(self, post, sleep):
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy('B')]), DAY, PREFS)
        key = vacancy('B')['id']
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy('B', detail_complete=False)], complete=False, errors=['timeout']), DAY, PREFS)
        self.state['outbox'] = {k: v for k, v in self.state['outbox'].items() if v['kind'] == 'new'}
        deliver(self.state, DAY, lambda: None)
        post.assert_not_called()
        self.assertEqual(self.state['outbox']['new:' + key]['state'], 'pending')
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy('B')]), DAY, PREFS)
        post.return_value = Mock(status_code=200, json=lambda: {'id': 'ack1'})
        self.assertEqual(deliver(self.state, DAY, lambda: None), 1)
        self.assertEqual(deliver(self.state, DAY, lambda: None), 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json']['allowed_mentions'], {'parse': []})

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/123/test_token'})
    @patch('monitor.notify.requests.post')
    def test_failed_discord_ack_remains_pending(self, post):
        import requests
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy('B')]), DAY, PREFS)
        post.side_effect = requests.Timeout()
        self.assertEqual(deliver(self.state, DAY, lambda: None), 0)
        self.assertEqual(self.state['outbox']['new:' + vacancy('B')['id']]['state'], 'pending')

    def test_roundtrip_and_excel_formula_defence(self):
        job = self.state['jobs'][vacancy()['id']]
        job['title'] = '=HYPERLINK("https://example.test")'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root/'data/store.json', self.state)
            restored = load_store(root/'data/store.json')
            self.assertEqual(restored, self.state)
            export(restored, [SOURCE], PREFS, root, DAY)
            raw = (root/'dist/data/jobs.csv').read_bytes()
            self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
            self.assertIn("'=HYPERLINK", raw.decode('utf-8-sig'))

    @patch.dict('os.environ', {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/123/test_token'})
    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_health_messages_choose_timestamp_not_sorted_key(self, post, sleep):
        self.state['sources']['test']['status'] = 'partial'
        self.state['outbox'] = {
            'health:a': {'id':'health:a', 'kind':'health', 'source_id':'test', 'state':'pending', 'created_at':'2026-09-05T02:00:00+00:00', 'errors':['newest failure']},
            'health:z': {'id':'health:z', 'kind':'health', 'source_id':'test', 'state':'pending', 'created_at':'2026-09-05T01:00:00+00:00', 'errors':['older failure']},
        }
        post.return_value = Mock(status_code=200, json=lambda: {'id':'ack2'})
        self.assertEqual(deliver(self.state, DAY, lambda: None), 1)
        self.assertIn('newest failure', post.call_args.kwargs['json']['content'])
        self.assertEqual(self.state['outbox']['health:z']['state'], 'superseded')


class AccessTests(unittest.TestCase):
    @patch('monitor.http.time.sleep')
    def test_public_get_retries_timeout_once_with_request_accounting(self, sleep):
        import requests
        client = PublicClient(SOURCE)
        reply = requests.Response()
        reply.status_code = 200
        reply.encoding = 'utf-8'
        reply._content = b'ok'
        reply._content_consumed = True
        client.session.request = Mock(side_effect=[requests.ConnectTimeout(), reply])
        self.assertEqual(client._request(SOURCE['url']).content, b'ok')
        self.assertEqual(client.count, 2)
        self.assertEqual(client.session.request.call_count, 2)
        client.session.request = Mock(side_effect=requests.ConnectTimeout())
        with self.assertRaises(CrawlError):
            client._request(SOURCE['url'])
        self.assertEqual(client.session.request.call_count, 2)

    def test_robots_and_post_timeout_are_not_automatically_retried(self):
        import requests
        for options in ({'robots':True}, {'method':'POST','data':{}}):
            client = PublicClient(SOURCE)
            client.session.request = Mock(side_effect=requests.ConnectTimeout())
            with self.assertRaises(CrawlError):
                client._request(SOURCE['url'], **options)
            self.assertEqual(client.session.request.call_count, 1)

    def test_access_denial_is_not_retried(self):
        client = PublicClient(SOURCE)
        client.allowed = Mock(return_value=True)
        client.session.request = Mock(return_value=Mock(status_code=403, headers={}, iter_content=lambda _: iter([b'blocked']), encoding='utf-8'))
        with self.assertRaises(CrawlError):
            client.response(SOURCE['url'])
        self.assertEqual(client.session.request.call_count, 1)

    @patch('monitor.http.time.sleep')
    def test_connection_reset_has_one_bounded_get_retry(self, sleep):
        import requests
        client = PublicClient(SOURCE)
        good = Mock(status_code=200, headers={}, iter_content=lambda _: iter([b'OK']), encoding='utf-8')
        client.session.request = Mock(side_effect=[requests.ConnectionError(), good])
        self.assertEqual(client._request(SOURCE['url']).status_code, 200)
        self.assertEqual(client.session.request.call_count, 2)
        client.session.request = Mock(side_effect=requests.ConnectionError())
        with self.assertRaises(CrawlError):
            client._request(SOURCE['url'])
        self.assertEqual(client.session.request.call_count, 2)
        for error, options in [(requests.exceptions.SSLError(), {}), (requests.exceptions.ProxyError(), {}),
                               (requests.ConnectionError(), {'robots': True}), (requests.ConnectionError(), {'method': 'POST', 'data': {}})]:
            client.session.request = Mock(side_effect=error)
            with self.assertRaises(CrawlError):
                client._request(SOURCE['url'], **options)
            self.assertEqual(client.session.request.call_count, 1)

    def test_robots_wildcards_longest_allow_and_bom(self):
        rules = RobotsPolicy('\ufeffUser-agent: *\nDisallow: /private\nAllow: /private/public\nDisallow: /*?secret=\nDisallow: /closed$')
        self.assertFalse(rules.can_fetch('https://example.edu/private/data'))
        self.assertTrue(rules.can_fetch('https://example.edu/private/public/jobs'))
        self.assertFalse(rules.can_fetch('https://example.edu/jobs?secret=1'))
        self.assertFalse(rules.can_fetch('https://example.edu/closed'))
        self.assertTrue(rules.can_fetch('https://example.edu/closed/jobs'))

    def test_robots_specific_groups_and_allow_tie(self):
        rules = RobotsPolicy('User-agent: *\nDisallow: /\nUser-agent: TeachingJobRadar\nDisallow: /jobs\nUser-agent: TeachingJobRadar\nAllow: /jobs')
        self.assertTrue(rules.can_fetch('https://example.edu/jobs'))

    def test_robots_failure_stays_denied(self):
        client = PublicClient(SOURCE)
        client._request = Mock(side_effect=CrawlError('timeout'))
        for _ in range(2):
            with self.assertRaises(CrawlError):
                client.allowed(SOURCE['url'])
        self.assertEqual(client._request.call_count, 1)

    def test_robots_challenge_is_not_empty_permission(self):
        client = PublicClient(SOURCE)
        client._request = Mock(return_value=Mock(status_code=200, text='{"captcha":true}', headers={'x-amzn-waf-action': 'captcha'}))
        with self.assertRaises(CrawlError):
            client.allowed(SOURCE['url'])

    def test_unapproved_host_never_requested(self):
        client = PublicClient(SOURCE)
        with self.assertRaises(CrawlError):
            client.get('https://another.test/jobs')
        self.assertEqual(client.count, 0)

    def test_pagination_denial_stops_before_details(self):
        source = {**SOURCE, 'adapter': 'hkbu_sce'}
        html = '<table class="table--job"><tbody><tr><td class="job-title"><a href="/job/1">Lecturer</a><span class="job-code">1</span></td><td>2026-09-05</td><td>Ongoing</td></tr></tbody></table><div class="pagination"><a href="/jobs?paged=2">2</a></div>'
        client = Mock()
        client.get.side_effect = [html, CrawlError('HTTP 403', stop_source=True)]
        batch = collect(source, client)
        self.assertFalse(batch.complete)
        self.assertEqual(len(batch.jobs), 1)
        self.assertEqual(client.get.call_count, 2)

    def test_unknown_html_is_not_empty_success(self):
        with self.assertRaises(CrawlError):
            parse_uow(SOURCE, '<html><title>New layout</title></html>')


class SourceSemanticsTests(unittest.TestCase):
    def test_schedule_label_tracks_workflow_instead_of_stale_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'.github/workflows/daily.yml'
            path.parent.mkdir(parents=True)
            path.write_text("on:\n  schedule:\n    - cron: '5 16 * * *'\n")
            self.assertIn('00:05', schedule_description(root))
            path.write_text("on:\n  schedule:\n    - cron: '30 21 * * *'\n      timezone: Asia/Hong_Kong\n")
            self.assertIn('21:30', schedule_description(root))
            path.write_text("on:\n  schedule:\n    - cron: '*/5 * * * *'\n")
            self.assertIn('未能確認', schedule_description(root))
    def test_explicit_dates_only_and_timezone(self):
        self.assertIsNone(parse_date('30+ days ago'))
        self.assertIsNone(parse_date('job reference 260905001'))
        self.assertEqual(parse_date('4-Sep-2026'), '2026-09-04')
        self.assertEqual(local_date('2026-09-04T23:00:00Z'), '2026-09-05')

    def test_deadline_review_untilfilled(self):
        self.assertEqual(deadline_from('Initial screening: 1 September 2026')[1], 'review')
        self.assertEqual(deadline_from('Closing date: Until the post is filled')[1], 'until-filled')
        self.assertEqual(deadline_from('Closing date: 10 September 2026 or until filled, whichever is earlier')[:2], ('2026-09-10', 'closing'))

    def test_oracle_inner_total_controls_pagination(self):
        jobs, total = parse_hkbu_page({**SOURCE, 'url':'https://example.edu/sites/hkbu/jobs'}, {'count':1, 'hasMore':False, 'items':[{'TotalJobsCount':146, 'requisitionList':[{'Id':'26270038','Title':'Part-time Lecturer','PostedDate':'2026-09-01'}]}]})
        self.assertEqual(total, 146)
        self.assertEqual(len(jobs), 1)


if __name__ == '__main__':
    unittest.main()
