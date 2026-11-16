import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.adapters import Batch
from monitor.model import record
from monitor.run import fetch_source, read_config
from monitor.store import export, fresh_store, reconcile


ROOT = Path(__file__).resolve().parents[1]
_, PREFS = read_config(ROOT)
SOURCE = {'id': 'review', 'group': 'review', 'institution': 'Review University',
          'name': 'Review', 'adapter': 'catalog_html', 'url': 'https://example.edu/jobs',
          'allowed_hosts': ['example.edu'], 'enabled': True, 'notes': ''}
NOW = '2026-11-16T18:00:00+00:00'  # November 17 in Hong Kong.


def vacancy(reference, **values):
    return record(SOURCE, 'Part-time Lecturer in Philosophy', 'https://example.edu/jobs/' + reference,
                  reference, description='Teaching philosophy and ethics.', employment_type='part-time', **values)


class ListingAndDetailsTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_store()
        reconcile(self.state, SOURCE, Batch(jobs=[vacancy('A'), vacancy('B')]), NOW, PREFS)

    def test_complete_listing_can_retire_absent_job_despite_detail_failure(self):
        for number in range(3):
            batch = Batch(jobs=[vacancy('B', detail_complete=False)], complete=False,
                          listing_complete=True, errors=['detail HTTP 504'])
            summary = reconcile(self.state, SOURCE, batch, NOW, PREFS)
            old = self.state['jobs'][vacancy('A')['id']]
            self.assertEqual(old['missing_count'], number + 1)
            self.assertEqual(old['status'], 'missing' if number == 2 else 'open')
            self.assertTrue(summary['listing_complete'])
            self.assertEqual(summary['severity'], 'error')
        health = self.state['sources']['review']
        self.assertEqual(health['detail_complete_count'], 0)
        self.assertEqual(health['last_listing_success'], NOW)

    def test_count_mismatch_or_legacy_partial_never_proves_absence(self):
        for listing_complete in (False, None):
            for _ in range(4):
                reconcile(self.state, SOURCE, Batch(jobs=[vacancy('B')], complete=False,
                          listing_complete=listing_complete, errors=['Expected 119, found 116']), NOW, PREFS)
        self.assertEqual(self.state['jobs'][vacancy('A')['id']]['missing_count'], 0)
        self.assertEqual(self.state['jobs'][vacancy('A')['id']]['status'], 'open')

    def test_suspicious_listing_drop_blocks_retirement_even_when_details_partial(self):
        jobs = [vacancy(str(number)) for number in range(10)]
        reconcile(self.state, SOURCE, Batch(jobs=jobs, complete=False, listing_complete=True), NOW, PREFS)
        self.assertEqual(self.state['sources']['review']['last_listing_count'], 10)
        summary = reconcile(self.state, SOURCE, Batch(jobs=[jobs[0]], complete=False, listing_complete=True), NOW, PREFS)
        self.assertFalse(summary['listing_complete'])
        self.assertEqual(summary['severity'], 'error')
        self.assertEqual(self.state['jobs'][jobs[1]['id']]['missing_count'], 0)

    def test_expected_list_only_gap_warns_but_a_failed_listing_is_error(self):
        source = {**SOURCE, 'capability': 'list_only'}
        summary = reconcile(self.state, source, Batch(jobs=[vacancy('B', detail_complete=False)],
                            complete=False, listing_complete=True), NOW, PREFS)
        self.assertEqual(summary['severity'], 'warning')
        summary = reconcile(self.state, source, Batch(complete=False, listing_complete=False,
                            errors=['listing unavailable']), NOW, PREFS)
        self.assertEqual(summary['severity'], 'error')
        self.assertEqual(summary['status'], 'error')

    def test_manual_capability_cannot_hide_an_explicit_unexpected_error(self):
        source = {**SOURCE, 'capability': 'manual'}
        result = reconcile(self.state, source, Batch(complete=False, listing_complete=False,
                           errors=['Official platform disallows crawling']), NOW, PREFS)
        self.assertEqual((result['status'], result['severity']), ('manual', 'warning'))
        result = reconcile(self.state, source, Batch(complete=False, listing_complete=False,
                           severity='error', errors=['Unexpected parser exception']), NOW, PREFS)
        self.assertEqual(result['severity'], 'error')

    def test_failed_refresh_keeps_verification_age_and_retry_marker(self):
        old = vacancy('B', detail_verified_at='2026-11-10T12:00:00+00:00', listing_fingerprint='old')
        reconcile(self.state, SOURCE, Batch(jobs=[old]), NOW, PREFS)
        incoming = vacancy('B', detail_complete=False, detail_refresh_failed=True)
        incoming['description'] = ''
        reconcile(self.state, SOURCE, Batch(jobs=[incoming], complete=False, listing_complete=True), NOW, PREFS)
        actual = self.state['jobs'][old['id']]
        self.assertEqual(actual['detail_verified_at'], '2026-11-10T12:00:00+00:00')
        self.assertEqual(actual['description'], old['description'])
        self.assertTrue(actual['detail_refresh_failed'])
        self.assertFalse(actual['detail_complete'])


class PublicFeedTests(unittest.TestCase):
    def test_recent_feed_and_full_archive_preserve_history_and_match_generation(self):
        state = fresh_store()
        jobs = [vacancy('boundary', posted_date='2026-10-19'),
                vacancy('too-old', posted_date='2026-10-18'),
                vacancy('closed', posted_date='2026-11-01', deadline='2026-11-02', deadline_type='closing'),
                vacancy('first-seen')]
        reconcile(state, SOURCE, Batch(jobs=jobs), NOW, PREFS)
        before = copy.deepcopy(state)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export(state, [SOURCE], PREFS, root, NOW)
            feed = json.loads((root / 'dist/data/jobs.json').read_text())
            archive = json.loads((root / 'dist/data/jobs-archive.json').read_text())
            self.assertEqual({job['reference'] for job in feed['jobs']}, {'boundary', 'closed', 'first-seen'})
            self.assertEqual(len(archive['jobs']), 4)
            self.assertEqual(feed['meta']['archive']['generated_at'], archive['meta']['generated_at'])
            self.assertEqual(feed['meta']['archive']['generation_id'], archive['meta']['generation_id'])
            self.assertEqual(feed['meta']['feed_days'], 30)
            self.assertEqual(archive['meta']['feed_scope'], 'full')
            self.assertEqual(feed['meta']['total_jobs'], 4)
            self.assertNotIn('archive', archive['meta'])
            self.assertTrue(all('content_hash' not in job and 'listing_fingerprint' not in job for job in archive['jobs']))
        self.assertEqual(state, before)

    @patch.dict('os.environ', {'RADAR_SCHEDULE_EXPECTED_AT': '2026-11-17T00:17:00Z',
                               'RADAR_SCHEDULE_STARTED_AT': '2026-11-17T06:00:00Z',
                               'RADAR_SCHEDULE_DELAY_SECONDS': '20580',
                               'RADAR_SCHEDULE_TIMING_BASIS': 'latest-daily-slot-before-monitor-start'})
    def test_schedule_metadata_keeps_measured_basis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export(fresh_store(), [SOURCE], PREFS, root, NOW)
            timing = json.loads((root / 'dist/data/jobs.json').read_text())['meta']['schedule_execution']
            self.assertEqual(timing['delay_seconds'], 20580)
            self.assertEqual(timing['timing_basis'], 'latest-daily-slot-before-monitor-start')

    @patch.dict('os.environ', {}, clear=True)
    def test_notification_only_export_retains_last_crawl_schedule_timing(self):
        state = fresh_store()
        timing = {'expected_at': '2026-11-17T00:17:00Z', 'started_at': '2026-11-17T06:00:00Z',
                  'delay_seconds': 20580, 'timing_basis': 'latest-daily-slot-before-monitor-start'}
        state['runs'].append({'status': 'degraded', 'schedule_execution': timing})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export(state, [SOURCE], PREFS, root, NOW)
            actual = json.loads((root / 'dist/data/jobs.json').read_text())['meta']['schedule_execution']
            self.assertEqual(actual, timing)


class FetchHealthTests(unittest.TestCase):
    @patch('monitor.run.collect')
    @patch('monitor.run.PublicClient')
    def test_injects_previous_jobs_and_reports_measured_requests(self, client_class, collect):
        client = client_class.return_value
        client.count, client.detail_cache_hits, client.detail_fetches = 3, 8, 1
        collect.return_value = Batch(jobs=[vacancy('A')])
        previous = [vacancy('A')]
        batch = fetch_source(SOURCE, previous, NOW)
        client.set_previous_jobs.assert_called_once_with(previous, NOW)
        self.assertEqual(batch.metrics, {'request_count': 3, 'detail_cache_hits': 8, 'detail_fetches': 1})

    @patch('monitor.run.collect', side_effect=ValueError('unknown parser layout'))
    @patch('monitor.run.PublicClient')
    def test_parser_exception_stays_error_even_for_configured_manual_source(self, client_class, collect):
        batch = fetch_source({**SOURCE, 'capability': 'manual'})
        self.assertEqual(batch.severity, 'error')
        self.assertFalse(batch.listing_complete)


if __name__ == '__main__':
    unittest.main()
