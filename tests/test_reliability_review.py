"""November 18 review: site-side failures, CUHK paging proof, HKU pacing,
list-only matching and the punctual daily trigger."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from monitor.adapters import Batch, collect
from monitor.http import CrawlError, PublicClient, RobotsPolicy, detail_jobs
from monitor.model import record
from monitor.notify import deliver, payload_for
from monitor.official import collect_cuhk
from monitor.rules import evaluate, role_of
from monitor.run import fetch_source, read_config
from monitor.store import fresh_store, reconcile
from scripts.run_scheduled import execution_environment, recent_full_run, trigger_kind

ROOT = Path(__file__).resolve().parents[1]
CONFIG, PREFS = read_config(ROOT)
SOURCES = {source['id']: source for source in CONFIG}
SOURCE = {'id': 'review', 'group': 'review', 'institution': 'Review University', 'name': 'Review',
          'adapter': 'catalog_html', 'url': 'https://example.edu/jobs', 'allowed_hosts': ['example.edu'],
          'enabled': True, 'notes': ''}
NOW = '2026-11-18T01:00:00+00:00'


def response(status, text='ok', headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = text.encode()
    result._content_consumed = True
    result.encoding = 'utf-8'
    result.headers.update(headers or {})
    return result


def vacancy(reference, source=SOURCE, title='Part-time Lecturer in Philosophy', **values):
    values.setdefault('description', 'Teaching philosophy and ethics.')
    values.setdefault('employment_type', 'part-time')
    return record(source, title, 'https://example.edu/jobs/' + reference, reference, **values)


def open_client(source=SOURCE):
    client = PublicClient(source)
    client.robots['https://example.edu'] = RobotsPolicy('User-agent: *\nAllow: /')
    return client


class FailureKindTests(unittest.TestCase):
    def test_waf_challenge_is_a_recorded_site_side_block(self):
        client = open_client()
        client._request = Mock(return_value=response(202, '', {'x-amzn-waf-action': 'challenge'}))
        with self.assertRaises(CrawlError) as caught:
            client.get('https://example.edu/jobs/1')
        self.assertEqual(caught.exception.kind, 'blocked')
        self.assertTrue(caught.exception.stop_source)
        self.assertIn('網站要求瀏覽器驗證（HTTP 202）', str(caught.exception))
        self.assertIn((str(caught.exception), 'blocked'), client.failures)

    def test_server_errors_are_network_and_moved_pages_need_a_person(self):
        for code, kind in [(503, 'network'), (404, None), (410, None), (403, 'blocked'), (429, 'blocked')]:
            with self.subTest(code=code):
                client = open_client()
                client._request = Mock(return_value=response(code))
                with self.assertRaises(CrawlError) as caught:
                    client.get('https://example.edu/jobs/1')
                self.assertEqual(caught.exception.kind, kind)

    def test_robots_check_page_names_cloudflare_and_later_requests_inherit_the_kind(self):
        client = PublicClient(SOURCE)
        client._request = Mock(return_value=response(200, '<!DOCTYPE html><title>Just a moment...</title>',
                                                     {'cf-ray': '123-HKG', 'server': 'cloudflare'}))
        with self.assertRaises(CrawlError) as caught:
            client.allowed('https://example.edu/jobs')
        self.assertEqual(caught.exception.kind, 'blocked')
        self.assertIn('Cloudflare', str(caught.exception))
        with self.assertRaises(CrawlError) as again:
            client.allowed('https://example.edu/jobs/2')
        self.assertEqual(again.exception.kind, 'blocked')
        self.assertEqual(client._request.call_count, 1)

    @patch('monitor.http.time.sleep')
    def test_timeouts_are_network(self, sleep):
        client = open_client()
        client.session.request = Mock(side_effect=requests.ConnectTimeout())
        with self.assertRaises(CrawlError) as caught:
            client.get('https://example.edu/jobs/1')
        self.assertEqual(caught.exception.kind, 'network')


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_store()
        self.jobs = [vacancy(str(number)) for number in range(8)]
        reconcile(self.state, SOURCE, Batch(jobs=self.jobs), NOW, PREFS)

    def test_blocked_listing_is_a_warning_without_retirement_or_drop_alarm(self):
        def blocked(source, client):
            raise client._error('robots.txt 回傳網頁、驗證頁或其他非規則內容（HTTP 200）（Cloudflare 驗證頁），未能確認自動讀取規則；已保留舊資料。',
                                kind='blocked', stop_source=True)
        with patch('monitor.run.collect', side_effect=blocked):
            batch = fetch_source(SOURCE)
        self.assertTrue(batch.external_failure)
        self.assertEqual(batch.severity, 'warning')
        summary = reconcile(self.state, SOURCE, batch, NOW, PREFS)
        self.assertEqual((summary['status'], summary['severity']), ('unreachable', 'warning'))
        health = self.state['sources']['review']
        self.assertNotIn('職位數量大幅減少，暫不判定舊職位消失。', health['errors'])
        self.assertTrue(all(job['missing_count'] == 0 and job['status'] == 'open' for job in self.state['jobs'].values()))

    def test_layout_change_stays_an_error(self):
        with patch('monitor.run.collect', side_effect=CrawlError('HKU 職位表格欄位有變。')):
            batch = fetch_source(SOURCE)
        self.assertFalse(batch.external_failure)
        self.assertEqual(batch.severity, 'error')
        self.assertEqual(reconcile(self.state, SOURCE, batch, NOW, PREFS)['severity'], 'error')

    def test_caught_detail_block_with_complete_listing_is_partial_warning_and_still_retires(self):
        def details_blocked(source, client):
            error = client._error('網站要求瀏覽器驗證（HTTP 202）。', kind='blocked', stop_source=True)
            jobs = [vacancy(str(number), detail_complete=False) for number in range(1, 8)]
            return Batch(jobs=jobs, complete=False, listing_complete=True, errors=[str(error)])
        with patch('monitor.run.collect', side_effect=details_blocked):
            batch = fetch_source(SOURCE)
        self.assertTrue(batch.external_failure)
        summary = reconcile(self.state, SOURCE, batch, NOW, PREFS)
        self.assertEqual((summary['status'], summary['severity']), ('partial', 'warning'))
        self.assertEqual(self.state['jobs'][self.jobs[0]['id']]['missing_count'], 1)

    def test_one_untraceable_message_keeps_the_conservative_error(self):
        def mixed(source, client):
            error = client._error('www.example.edu 連線未完成（ReadTimeout）。', kind='network')
            return Batch(jobs=self.jobs, complete=False, listing_complete=True,
                         errors=[str(error), '詳情頁的內文格式未能確認。'])
        with patch('monitor.run.collect', side_effect=mixed):
            batch = fetch_source(SOURCE)
        self.assertFalse(batch.external_failure)
        self.assertEqual(reconcile(self.state, SOURCE, batch, NOW, PREFS)['severity'], 'error')

    def test_a_traced_client_failure_of_an_internal_kind_is_not_external(self):
        def moved(source, client):
            error = client._error('網站未提供可讀頁面（HTTP 404）。')  # a moved page needs a person
            return Batch(jobs=self.jobs, complete=False, listing_complete=True, errors=[str(error)])
        with patch('monitor.run.collect', side_effect=moved):
            batch = fetch_source(SOURCE)
        self.assertFalse(batch.external_failure)
        self.assertEqual(reconcile(self.state, SOURCE, batch, NOW, PREFS)['severity'], 'error')


class CuhkPagingTests(unittest.TestCase):
    """CUHK reported 3 more jobs than its pages returned on every day from
    October 6 to November 16 while the total moved between 116 and 124."""

    @staticmethod
    def taleo(desc_refs, total, asc_refs=None, size=25, fail_ascending=False):
        calls = []
        def search(endpoint, payload, headers):
            ascending = payload['sortingSelection']['ascendingSortingOrder'] == 'true'
            calls.append(ascending)
            if ascending and fail_ascending:
                raise CrawlError('cuhk.taleo.net 連線未完成（ReadTimeout）。', kind='network')
            refs = sorted(asc_refs if ascending and asc_refs is not None else desc_refs, reverse=not ascending)
            page = payload['pageNo']
            rows = refs[(page - 1) * size: page * size]
            return {'pagingData': {'currentPageNo': page, 'pageSize': size, 'totalCount': total},
                    'requisitionList': [{'contestNo': ref, 'linkedColumn': 0,
                                         'column': ['Lecturer', ref, 'Humanities', '']} for ref in rows]}
        return search, calls

    def collect(self, search):
        client = Mock()
        client.search_json.side_effect = search
        with patch('monitor.official.parse_cuhk_detail', side_effect=lambda job, html: job.update(detail_complete=True)):
            return collect_cuhk(SOURCES['cuhk'], client)

    def refs(self, count, start=1):
        return [f'R{number:03d}' for number in range(start, start + count)]

    def test_consistent_reverse_reading_proves_the_shortfall_is_unlisted(self):
        search, calls = self.taleo(self.refs(116), 119)
        batch = self.collect(search)
        self.assertTrue(batch.listing_complete)
        self.assertTrue(batch.complete)
        self.assertEqual(len(batch.jobs), 116)
        self.assertEqual(calls, [False] * 5 + [True] * 5)
        self.assertIn('兩次職位編號完全相同', batch.notes[0])
        self.assertEqual(batch.errors, [])

    def test_reverse_reading_that_finds_the_missing_rows_uses_them(self):
        search, _ = self.taleo(self.refs(116), 119, asc_refs=self.refs(119))
        batch = self.collect(search)
        self.assertTrue(batch.listing_complete)
        self.assertEqual(len(batch.jobs), 119)
        self.assertIn('已取得全部 119 個職位', batch.notes[0])

    def test_different_reverse_set_stays_partial(self):
        swapped = self.refs(115) + ['R900']
        search, _ = self.taleo(self.refs(116), 119, asc_refs=swapped)
        batch = self.collect(search)
        self.assertFalse(batch.listing_complete)
        self.assertFalse(batch.complete)
        self.assertIn('反向排序讀到 116 個職位，與第一次不完全相同', batch.errors[0])
        self.assertIn('暫不判定舊職位消失', batch.errors[0])

    def test_large_gap_or_duplicates_never_trigger_the_second_reading(self):
        search, calls = self.taleo(self.refs(100), 119)
        batch = self.collect(search)
        self.assertFalse(batch.listing_complete)
        self.assertNotIn(True, calls)
        refs = self.refs(116)
        def duplicate(endpoint, payload, headers):
            page = payload['pageNo']
            rows = sorted(refs, reverse=True)[(page - 1) * 25: page * 25]
            if page == 2:
                rows = rows[:-1] + [sorted(refs, reverse=True)[0]]
            return {'pagingData': {'currentPageNo': page, 'pageSize': 25, 'totalCount': 119},
                    'requisitionList': [{'contestNo': ref, 'linkedColumn': 0, 'column': ['Lecturer', ref, 'Humanities', '']} for ref in rows]}
        client = Mock()
        client.search_json.side_effect = duplicate
        with patch('monitor.official.parse_cuhk_detail', side_effect=lambda job, html: job.update(detail_complete=True)):
            batch = collect_cuhk(SOURCES['cuhk'], client)
        self.assertFalse(batch.listing_complete)
        self.assertEqual(client.search_json.call_count, 5)

    def test_rows_read_before_a_paging_failure_are_kept(self):
        refs = sorted(self.refs(116), reverse=True)
        for stop in (False, True):
            def search(endpoint, payload, headers, stop=stop):
                page = payload['pageNo']
                if page == 3:
                    raise CrawlError('cuhk.taleo.net 連線未完成（ReadTimeout）。', kind='network', stop_source=stop)
                rows = refs[(page - 1) * 25: page * 25]
                return {'pagingData': {'currentPageNo': page, 'pageSize': 25, 'totalCount': 119},
                        'requisitionList': [{'contestNo': ref, 'linkedColumn': 0, 'column': ['Lecturer', ref, 'Humanities', '']} for ref in rows]}
            with self.subTest(stop_source=stop):
                batch = self.collect(search)
                self.assertFalse(batch.listing_complete)
                self.assertEqual(len(batch.jobs), 50)
                self.assertEqual(batch.pages, 2)

    def test_failed_reverse_reading_keeps_the_count_diagnosis(self):
        search, _ = self.taleo(self.refs(116), 119, fail_ascending=True)
        batch = self.collect(search)
        self.assertFalse(batch.listing_complete)
        self.assertIn('官方回報 119 個職位', batch.errors[0])
        self.assertIn('反向排序核對未完成', batch.errors[0])


class PacingAndBudgetTests(unittest.TestCase):
    def test_minimum_interval_is_configurable_and_bounded(self):
        self.assertEqual(PublicClient(SOURCE).min_interval, 1.1)
        self.assertEqual(PublicClient({**SOURCE, 'min_request_interval': 80}).min_interval, 80)
        self.assertEqual(PublicClient({**SOURCE, 'min_request_interval': 900}).min_interval, 120)
        self.assertEqual(PublicClient({**SOURCE, 'min_request_interval': 0}).min_interval, 1.1)

    @patch('monitor.http.time.monotonic', return_value=1000.0)
    @patch('monitor.http.time.sleep')
    def test_same_host_requests_wait_the_configured_gap(self, sleep, monotonic):
        client = PublicClient({**SOURCE, 'min_request_interval': 80})
        client.session.request = Mock(return_value=response(200))
        client._request('https://example.edu/jobs')
        sleep.assert_not_called()
        client._request('https://example.edu/jobs/2')
        sleep.assert_called_once_with(80.0)

    def test_budget_reads_new_ads_first_and_defers_the_rest(self):
        client = PublicClient({**SOURCE, 'max_detail_fetches': 2})
        old = [vacancy(str(number), detail_complete=True, detail_verified_at='2026-10-01T00:00:00+00:00',
                       listing_fingerprint='old') for number in range(3)]
        client.set_previous_jobs(old, NOW)
        new = [vacancy('new-1', detail_complete=False), vacancy('new-2', detail_complete=False)]
        incoming = [vacancy(job['reference'], detail_complete=False) for job in old] + new
        queue = detail_jobs(client, incoming)
        self.assertEqual([job['reference'] for job in queue], ['new-1', 'new-2'])
        self.assertEqual(client.detail_deferred, 3)

    def test_budget_is_per_run_across_several_detail_queues(self):
        client = PublicClient({**SOURCE, 'max_detail_fetches': 3})
        first = detail_jobs(client, [vacancy(f'a{number}', detail_complete=False) for number in range(2)])
        second = detail_jobs(client, [vacancy(f'b{number}', detail_complete=False) for number in range(4)])
        self.assertEqual((len(first), len(second)), (2, 1))
        self.assertEqual(client.detail_deferred, 3)

    def test_deferred_details_are_reported_as_a_site_limit(self):
        source = {**SOURCE, 'max_detail_fetches': 1}
        def collector(source, client):
            jobs = [vacancy(str(number), detail_complete=False) for number in range(3)]
            for job in detail_jobs(client, jobs):
                job.update(detail_complete=True)
            return Batch(jobs=jobs, listing_complete=True)
        with patch('monitor.run.collect', side_effect=collector):
            batch = fetch_source(source)
        self.assertFalse(batch.complete)
        self.assertTrue(batch.listing_complete)
        self.assertTrue(batch.external_failure)
        self.assertIn('詳情分批更新', batch.errors[0])
        self.assertEqual(batch.metrics['detail_deferred'], 2)

    def test_hku_configuration_stays_within_the_observed_waf_allowance(self):
        hku = SOURCES['hku']
        # About four requests per five minutes, a bounded run, and a longer
        # reuse period so a full refresh fits in the daily allowance.
        self.assertGreaterEqual(hku['min_request_interval'], 75)
        self.assertLessEqual(hku['max_detail_fetches'] * hku['min_request_interval'], 15 * 60)
        self.assertLessEqual(hku['detail_cache_days'], 30)


class ListOnlyMatchingTests(unittest.TestCase):
    def cityu(self, reference, title, department='Department of Public and International Affairs'):
        return record(SOURCES['cityu'], title, 'https://www.cityu.edu.hk/hro/en/job/current/academic.asp?ref=' + reference,
                      reference, department=department, detail_complete=False, detail_unavailable=True,
                      employment_type='part-time')

    def test_title_and_department_evidence_is_enough_for_list_only_sources(self):
        result = evaluate(self.cityu('C1', 'Part-time Lecturers (Philosophy)'), PREFS)
        self.assertTrue(result['matches'])
        self.assertEqual(result['match_basis'], 'title')
        self.assertFalse(evaluate(self.cityu('C2', 'Part-time Lecturers (Data Science)', 'Department of Data Science'), PREFS)['matches'])

    def test_new_list_only_match_is_queued_but_temporary_gaps_still_wait(self):
        state = fresh_store()
        reconcile(state, SOURCES['cityu'], Batch(jobs=[self.cityu('C0', 'Part-time Lecturers (Physics)')]), NOW, PREFS)
        listed = self.cityu('C1', 'Part-time Lecturers (Philosophy)')
        reconcile(state, SOURCES['cityu'], Batch(jobs=[self.cityu('C0', 'Part-time Lecturers (Physics)'), listed],
                                                 complete=False, listing_complete=True, capability='list_only',
                                                 severity='warning'), NOW, PREFS)
        self.assertEqual(state['outbox']['new:' + listed['id']]['state'], 'pending')
        waiting = fresh_store()
        reconcile(waiting, SOURCE, Batch(jobs=[vacancy('A')]), NOW, PREFS)
        unread = vacancy('B', detail_complete=False, description='')
        reconcile(waiting, SOURCE, Batch(jobs=[vacancy('A'), unread], complete=False, listing_complete=True), NOW, PREFS)
        self.assertFalse(waiting['jobs'][unread['id']]['matches'])
        self.assertNotIn('new:' + unread['id'], waiting['outbox'])

    @patch('monitor.notify.time.sleep')
    @patch('monitor.notify.requests.post')
    def test_discord_message_says_the_match_used_the_title(self, post, sleep):
        state = fresh_store()
        job = {**self.cityu('C1', 'Part-time Lecturers (Philosophy)'), 'status': 'open', 'first_seen': NOW}
        job.update(evaluate(job, PREFS))
        state['jobs'][job['id']] = job
        event = {'id': 'new:' + job['id'], 'kind': 'new', 'job_id': job['id'], 'created_at': NOW, 'state': 'pending'}
        state['outbox'][event['id']] = event
        self.assertIn('只提供清單', payload_for(event, state)['content'])
        post.return_value = Mock(status_code=200, json=Mock(return_value={'id': '1'}))
        with patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/1/abc'}):
            self.assertEqual(deliver(state, NOW, lambda: None), 1)
        self.assertEqual(state['outbox'][event['id']]['state'], 'sent')

    def test_teaching_titles_that_were_missed(self):
        for title, role in [('Part-time Tutorship (2027 Spring Term starting in January) (E&L)', 'tutor'),
                            ('Part-time Teaching Associate (Computer Studies)', 'lecturer'),
                            ('Teaching Consultant (Crime Science/Criminology & Sociology)', 'lecturer'),
                            ('日間兼職教導員（屋宇裝備工程）', 'instructor'),
                            ('Part-time Teaching Staff (Philosophy)', 'lecturer'),
                            ('Part-time Teaching Assistant', 'other'),
                            ('Part-time Teaching Practice Supervisors of EDU 4302ECF', 'other')]:
            with self.subTest(title=title):
                self.assertEqual(role_of(title), role)


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.github/workflows').mkdir(parents=True)
        (self.root / 'config').mkdir()
        (self.root / 'data').mkdir()
        (self.root / '.github/workflows/daily.yml').write_text("on:\n  schedule:\n    - cron: '17 0 * * *'\n")
        (self.root / 'config/sites.yaml').write_text('sources:\n- id: a\n  enabled: true\n- id: b\n  enabled: true\n- id: c\n  enabled: false\n')
        self.now = datetime(2026, 11, 18, 0, 20, tzinfo=timezone.utc)

    def write_runs(self, *runs):
        (self.root / 'data/store.json').write_text(json.dumps({'version': 1, 'jobs': {}, 'runs': list(runs)}))

    def run_at(self, hours_ago, sources=('a', 'b')):
        started = (self.now - timedelta(hours=hours_ago)).isoformat()
        return {'started_at': started, 'sources': [{'source_id': source} for source in sources]}

    def test_outside_timer_counts_as_the_daily_schedule(self):
        self.assertEqual(trigger_kind('schedule', {}), 'schedule')
        self.assertEqual(trigger_kind('workflow_dispatch', {'RADAR_TRIGGER': 'schedule'}), 'schedule')
        self.assertEqual(trigger_kind('workflow_dispatch', {}), 'workflow_dispatch')
        env = execution_environment('workflow_dispatch', self.root, self.now, {'RADAR_TRIGGER': 'schedule'})
        self.assertEqual(env['RADAR_SCHEDULE_TRIGGER'], 'external-schedule')
        self.assertEqual(env['RADAR_SCHEDULE_DELAY_SECONDS'], '180')
        self.assertEqual(execution_environment('schedule', self.root, self.now, {})['RADAR_SCHEDULE_TRIGGER'], 'github-schedule')

    def test_only_a_full_run_since_todays_slot_makes_the_backup_skip(self):
        # The GitHub backup fires at 15:00 Hong Kong time for the 08:17 slot.
        late = datetime(2026, 11, 18, 7, 0, tzinfo=timezone.utc)
        def run_at(hour, day=18, sources=('a', 'b')):
            return {'started_at': datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc).isoformat(),
                    'sources': [{'source_id': source} for source in sources]}
        cases = [((run_at(0),), True),                       # punctual outside timer, 08:00 HKT
                 ((run_at(23, day=17),), True),               # within the two-hour tolerance
                 ((run_at(20, day=17),), False),              # evening code push the day before
                 ((run_at(1, sources=('a',)),), False),       # targeted repair run
                 ((run_at(20, day=17), run_at(1, sources=('b',))), False)]
        for runs, skip in cases:
            with self.subTest(runs=[run['started_at'] for run in runs]):
                self.write_runs(*runs)
                self.assertEqual(recent_full_run(self.root, late) is not None, skip)
        (self.root / 'data/store.json').write_text('{not json')
        self.assertIsNone(recent_full_run(self.root, late))

    def test_unreadable_cron_falls_back_to_a_short_window(self):
        (self.root / '.github/workflows/daily.yml').write_text("on:\n  schedule:\n    - cron: '*/30 * * * *'\n")
        self.write_runs(self.run_at(11))
        self.assertIsNotNone(recent_full_run(self.root, self.now))
        self.write_runs(self.run_at(13))
        self.assertIsNone(recent_full_run(self.root, self.now))


class ManualSourceTests(unittest.TestCase):
    def test_nang_yan_is_manual_with_its_own_reason_and_no_requests(self):
        client = Mock()
        batch = collect(SOURCES['nangyan'], client)
        self.assertEqual(client.mock_calls, [])
        self.assertEqual((batch.capability, batch.severity), ('manual', 'warning'))
        self.assertIn('連線一直逾時', batch.errors[0])
        self.assertIn('本輪沒有發送', batch.errors[0])


if __name__ == '__main__':
    unittest.main()
