"""November 19 trial follow-up: a source that fails on the program side is
read once more at the end of the round, and reconciled once."""
import threading
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.adapters import Batch
from monitor.http import CrawlError
from monitor.model import record
from monitor.run import (RETRY_BUDGET_SECONDS, RETRY_GAP_SECONDS, crawl, fetch_source, preview,
                         read_config, retry_when_due)
from monitor.store import fresh_store, reconcile

ROOT = Path(__file__).resolve().parents[1]
_, PREFS = read_config(ROOT)
EARLIER = '2026-11-18T01:00:00+00:00'
NOW = '2026-11-19T01:00:00+00:00'


def site(name, **extra):
    return {'id': name, 'group': name, 'institution': name.title() + ' College', 'name': name,
            'adapter': 'catalog_html', 'url': f'https://{name}.example.edu/jobs',
            'allowed_hosts': [f'{name}.example.edu'], 'enabled': True, 'notes': '', **extra}


ALPHA, BETA = site('alpha'), site('beta')


def job(source, reference, **values):
    values.setdefault('description', 'Teaching philosophy and ethics.')
    values.setdefault('employment_type', 'part-time')
    return record(source, 'Part-time Lecturer in Philosophy',
                  f"{source['url']}/{reference}", reference, **values)


def good(source, refs=('A1', 'A2', 'A3')):
    return Batch(jobs=[job(source, ref) for ref in refs], complete=True, listing_complete=True,
                 metrics={'request_count': 4})


def unproven_listing(source, refs=('A1', 'A2')):
    return Batch(jobs=[job(source, ref) for ref in refs], complete=False, listing_complete=False,
                 errors=['清單核對未通過：官方總數 3，只讀到 2。'], metrics={'request_count': 5})


def seeded(*sources):
    state = fresh_store()
    for source in sources:
        reconcile(state, source, good(source), EARLIER, PREFS)
    return state


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.lock = threading.Lock()

    def clock(self):
        return self.now

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.now += seconds


class Script:
    """fetch_source stand-in: each source answers with its next scripted batch."""

    def __init__(self, answers, before=None):
        self.answers = {key: list(value) for key, value in answers.items()}
        self.calls = defaultdict(list)
        self.before = before or {}
        self.lock = threading.Lock()

    def __call__(self, source, cached_jobs=(), now=None):
        with self.lock:
            self.calls[source['id']].append(list(cached_jobs))
            attempt = len(self.calls[source['id']])
            answer = self.answers[source['id']].pop(0)
        hook = self.before.get((source['id'], attempt))
        if hook:
            hook()
        return answer


def run_round(state, sources, script, fake=None):
    fake = fake or FakeTime()
    save = Mock()
    with patch('monitor.run.fetch_source', side_effect=script), \
            patch('monitor.run.clock', fake.clock), patch('monitor.run.sleep', fake.sleep):
        summaries = crawl(state, sources, NOW, PREFS, save)
    return {summary['source_id']: summary for summary in summaries}, fake, save


class EndOfRoundRetryTests(unittest.TestCase):
    def test_a_program_side_failure_is_read_again_and_clears_without_an_alert(self):
        state = seeded(ALPHA, BETA)
        script = Script({'alpha': [unproven_listing(ALPHA), good(ALPHA)], 'beta': [good(BETA)]})
        summaries, fake, save = run_round(state, [ALPHA, BETA], script)
        self.assertEqual((len(script.calls['alpha']), len(script.calls['beta'])), (2, 1))
        self.assertEqual(sorted(summaries), ['alpha', 'beta'])
        self.assertEqual((summaries['alpha']['severity'], summaries['alpha']['listing_complete']), ('ok', True))
        self.assertEqual(summaries['alpha']['metrics']['retry_attempts'], 1)
        self.assertEqual(summaries['alpha']['metrics']['first_attempt_request_count'], 5)
        notes = ' '.join(state['sources']['alpha']['run_notes'])
        self.assertIn('自動重試', notes)
        self.assertIn('清單核對未通過', notes)
        self.assertEqual(state['sources']['alpha']['errors'], [])
        # The retry waited out the gap, and nothing was queued for Discord.
        self.assertEqual(fake.sleeps, [RETRY_GAP_SECONDS])
        self.assertFalse([event for event in state['outbox'].values() if event.get('source_id') == 'alpha'])
        self.assertEqual(save.call_count, 2)

    def test_the_preview_that_decides_a_retry_leaves_the_store_untouched(self):
        state = seeded(ALPHA)
        before = repr(sorted(state['jobs'].items())), repr(state['sources']), len(state['history'])
        outlook = preview(state, ALPHA, unproven_listing(ALPHA), NOW, PREFS)
        self.assertEqual(outlook['severity'], 'error')
        self.assertEqual((repr(sorted(state['jobs'].items())), repr(state['sources']), len(state['history'])), before)
        self.assertEqual(state['outbox'], {})

    def test_without_a_retry_the_same_failure_raises_an_alert(self):
        state = seeded(ALPHA)
        summary = reconcile(state, ALPHA, unproven_listing(ALPHA), NOW, PREFS)
        self.assertEqual(summary['severity'], 'error')
        self.assertTrue([event for event in state['outbox'].values() if event.get('source_id') == 'alpha'])

    def test_a_site_that_asked_for_a_browser_check_or_a_slower_pace_is_not_read_again(self):
        for kind in ('blocked', 'deferred'):
            with self.subTest(kind=kind):
                state = seeded(ALPHA)
                first = unproven_listing(ALPHA)
                first.failure_kinds = [kind]
                script = Script({'alpha': [first]})
                summaries, fake, _ = run_round(state, [ALPHA], script)
                self.assertEqual(len(script.calls['alpha']), 1)
                self.assertEqual(summaries['alpha']['severity'], 'error')
                self.assertEqual(fake.sleeps, [])

    def test_a_manual_source_is_not_read_again(self):
        source = site('gamma', adapter='availability_check', capability='manual')
        state = seeded()
        first = Batch(jobs=[], complete=False, listing_complete=False, capability='manual',
                      severity='warning', errors=['請自行查看官方網站。'])
        script = Script({'gamma': [first]})
        summaries, _, _ = run_round(state, [source], script)
        self.assertEqual(len(script.calls['gamma']), 1)
        self.assertEqual(summaries['gamma']['status'], 'manual')

    def test_a_round_where_connections_failed_and_nothing_was_read_is_tried_again(self):
        state = seeded(ALPHA)
        first = Batch(jobs=[], complete=False, listing_complete=False, errors=['連線逾時，網站暫時未能回應。'],
                      external_failure=True, failure_kinds=['network'])
        script = Script({'alpha': [first, good(ALPHA)]})
        summaries, _, _ = run_round(state, [ALPHA], script)
        self.assertEqual(len(script.calls['alpha']), 2)
        self.assertEqual(summaries['alpha']['status'], 'ok')

    def test_a_worse_second_attempt_does_not_replace_the_first(self):
        state = seeded(ALPHA)
        first = Batch(jobs=[job(ALPHA, 'A1'), job(ALPHA, 'A2', detail_complete=False, description='')],
                      complete=False, listing_complete=True, metrics={'request_count': 6})
        second = Batch(jobs=[], complete=False, listing_complete=False, errors=['清單讀取失敗。'])
        script = Script({'alpha': [first, second]})
        summaries, _, _ = run_round(state, [ALPHA], script)
        self.assertEqual(len(script.calls['alpha']), 2)
        self.assertEqual((summaries['alpha']['count'], summaries['alpha']['listing_complete']), (2, True))
        self.assertIn('未有改善', ' '.join(state['sources']['alpha']['run_notes']))
        self.assertNotIn('清單讀取失敗。', state['sources']['alpha']['errors'])

    def test_the_retry_reuses_details_the_first_attempt_read_in_full(self):
        state = seeded(ALPHA)
        fresh = job(ALPHA, 'A4', description='Newly advertised philosophy tutorship.')
        missing = job(ALPHA, 'A5', detail_complete=False, description='')
        first = Batch(jobs=[fresh, missing], complete=False, listing_complete=True)
        script = Script({'alpha': [first, good(ALPHA, ('A4', 'A5'))]})
        run_round(state, [ALPHA], script)
        cache = {item['id']: item for item in script.calls['alpha'][1]}
        self.assertTrue(cache[fresh['id']]['detail_complete'])
        self.assertEqual(cache[fresh['id']]['description'], 'Newly advertised philosophy tutorship.')
        self.assertNotIn(missing['id'], cache)
        self.assertEqual(len(cache), 4)  # the three earlier records plus A4

    def test_no_retry_when_it_would_not_finish_within_the_round(self):
        state = seeded(ALPHA)
        fake = FakeTime()

        def slow_first_attempt():
            fake.now += RETRY_BUDGET_SECONDS - 60

        script = Script({'alpha': [unproven_listing(ALPHA)]}, before={('alpha', 1): slow_first_attempt})
        summaries, fake, _ = run_round(state, [ALPHA], script, fake)
        self.assertEqual(len(script.calls['alpha']), 1)
        self.assertEqual(summaries['alpha']['severity'], 'error')
        self.assertIn('時間不足', ' '.join(state['sources']['alpha']['run_notes']))
        self.assertEqual(fake.sleeps, [])

    def test_the_retry_waits_only_for_what_is_left_of_the_gap(self):
        first = unproven_listing(ALPHA)
        for current, expected in ((150.0, []), (40.0, [60.0])):
            with self.subTest(current=current):
                fake = FakeTime()
                fake.now = current
                with patch('monitor.run.clock', fake.clock), patch('monitor.run.sleep', fake.sleep), \
                        patch('monitor.run.fetch_source', return_value=good(ALPHA)) as fetch:
                    result = retry_when_due(ALPHA, [], first, due=100.0, deadline=10_000.0, duration=5.0, now=NOW)
                self.assertEqual(fake.sleeps, expected)
                self.assertEqual(fetch.call_count, 1)
                self.assertTrue(result.complete)


class RedirectMessageTests(unittest.TestCase):
    def test_a_refused_address_names_its_host_but_not_its_path_query_or_credentials(self):
        from monitor.http import PublicClient
        client = PublicClient(ALPHA)
        cases = {'http://alpha.example.edu/jobs?token=secret': 'http://alpha.example.edu',
                 'https://user:pw@moved.example.org:8443/careers/list': 'https://moved.example.org:8443',
                 'https://elsewhere.example.net/a/b': 'https://elsewhere.example.net'}
        for url, shown in cases.items():
            with self.subTest(url=url):
                with self.assertRaises(CrawlError) as caught:
                    client.validate(url)
                message = str(caught.exception)
                self.assertIn(f'（{shown}）', message)
                for hidden in ('token', 'secret', 'careers', 'user', 'pw@', '/a/b'):
                    self.assertNotIn(hidden, message)
                self.assertTrue(caught.exception.stop_source)
        client.validate('https://alpha.example.edu/jobs/1')


class HsuMoveTests(unittest.TestCase):
    """HSU's list moved to its HR Office site on November 19; the links did not."""

    def test_the_hr_office_page_is_approved_and_its_links_still_parse(self):
        from monitor.hsu import parse_hsu_list
        configured = {item['id']: item for item in read_config(ROOT)[0]}['hsu']
        self.assertIn('hro.hsu.edu.hk', configured['allowed_hosts'])
        page = ('<h1>HSUHK Job Openings</h1><h3>Academic</h3><ul>'
                '<li><a href="https://recruit.hsu.edu.hk/opening/content.php?id=3925">'
                'Department of English – Lecturer(s)</a></li></ul><h3>Research</h3><ul>'
                '<li><a href="https://recruit.hsu.edu.hk/opening/content.php?id=3927">'
                'Part-time Research Assistant – Department of English</a></li>'
                '<li><a href="https://hro.hsu.edu.hk/equal-opportunity-employer/">Equal opportunity</a></li></ul>')
        jobs = parse_hsu_list(configured, page)
        self.assertEqual(sorted(job['reference'] for job in jobs), ['3925', '3927'])
        self.assertTrue(all(job['url'].startswith('https://recruit.hsu.edu.hk/opening/content.php?id=') for job in jobs))


class FailureKindRecordTests(unittest.TestCase):
    def test_a_raised_block_is_recorded_on_the_batch(self):
        with patch('monitor.run.collect', side_effect=CrawlError('網站要求瀏覽器驗證（HTTP 202）', kind='blocked')):
            batch = fetch_source(ALPHA)
        self.assertEqual(batch.failure_kinds, ['blocked'])

    def test_a_failure_the_client_recorded_is_kept_even_when_the_adapter_carried_on(self):
        def collector(source, client):
            client.record_failure('連線逾時', 'network')
            return Batch(jobs=[job(source, 'A1')], complete=False, listing_complete=False, errors=['連線逾時'])

        with patch('monitor.run.collect', side_effect=collector):
            batch = fetch_source(ALPHA)
        self.assertEqual(batch.failure_kinds, ['network'])

    def test_an_internal_error_records_no_kind(self):
        with patch('monitor.run.collect', side_effect=CrawlError('職位表格欄位有變。')):
            batch = fetch_source(ALPHA)
        self.assertEqual(batch.failure_kinds, [])


if __name__ == '__main__':
    unittest.main()
