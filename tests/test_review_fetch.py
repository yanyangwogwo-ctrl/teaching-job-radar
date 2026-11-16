import copy
import unittest
from unittest.mock import Mock, patch

import requests

from monitor.adapters import Batch
from monitor.catalog import read_details
from monitor.http import CrawlError, PublicClient, detail_jobs, tls_failure_reason
from monitor.model import record


SOURCE = {'id': 'test', 'institution': 'Test', 'group': 'test',
          'url': 'https://example.edu/jobs', 'allowed_hosts': ['example.edu']}
NOW = '2026-11-17T00:00:00+00:00'


def response(status, text='ok', headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = text.encode()
    result._content_consumed = True
    result.encoding = 'utf-8'
    result.headers.update(headers or {})
    return result


def listing(**changes):
    job = record(SOURCE, 'Lecturer', 'https://example.edu/job/123', '123', detail_complete=False)
    job.update(changes)
    return job


class RetryReviewTests(unittest.TestCase):
    def test_tls_diagnostics_report_reason_without_leaking_exception_details(self):
        self.assertIn('簽發者', tls_failure_reason('CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate https://secret.example/token'))
        self.assertIn('已過期', tls_failure_reason('CERTIFICATE_VERIFY_FAILED: certificate has expired'))
        self.assertIn('主機名稱不符', tls_failure_reason('CERTIFICATE_VERIFY_FAILED: hostname mismatch'))
        self.assertNotIn('secret.example', tls_failure_reason('CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate https://secret.example/token'))

    @patch('monitor.http.time.sleep')
    def test_504_retries_twice_and_keeps_request_count(self, sleep):
        client = PublicClient(SOURCE)
        client.session.request = Mock(side_effect=[response(504), response(503), response(200)])
        self.assertEqual(client._request(SOURCE['url']).status_code, 200)
        self.assertEqual(client.count, 3)
        self.assertEqual(client.session.request.call_count, 3)

    @patch('monitor.http.time.sleep')
    def test_retry_limits_include_robots_and_never_turn_failures_into_permission(self, sleep):
        client = PublicClient(SOURCE)
        client.session.request = Mock(side_effect=lambda *a, **k: response(503))
        for _ in range(2):
            with self.assertRaises(CrawlError):
                client.allowed(SOURCE['url'])
        self.assertEqual(client.session.request.call_count, 3)

    @patch('monitor.http.time.sleep')
    def test_post_denials_and_challenges_are_not_retried(self, sleep):
        for code, text, headers, method in [(503, 'error', {}, 'POST'),
                (403, 'denied', {}, 'GET'), (429, 'rate limited', {}, 'GET'),
                (202, 'challenge', {}, 'GET'), (503, '<title>Just a moment</title>', {}, 'GET'),
                (503, 'error', {'x-amzn-waf-action': 'challenge'}, 'GET')]:
            with self.subTest(code=code, method=method, text=text):
                client = PublicClient(SOURCE)
                client.session.request = Mock(return_value=response(code, text, headers))
                self.assertEqual(client._request(SOURCE['url'], method=method).status_code, code)
                self.assertEqual(client.session.request.call_count, 1)

    @patch('monitor.http.time.sleep')
    def test_retry_after_honored_without_retrying_early_after_long_wait(self, sleep):
        client = PublicClient(SOURCE)
        client.session.request = Mock(side_effect=[response(503, headers={'Retry-After': '7'}), response(200)])
        client._request(SOURCE['url'])
        self.assertIn(7.0, [call.args[0] for call in sleep.call_args_list])
        client.session.request = Mock(return_value=response(503, headers={'Retry-After': '120'}))
        self.assertEqual(client._request(SOURCE['url']).status_code, 503)
        self.assertEqual(client.session.request.call_count, 1)

    def test_robots_403_exception_is_host_scoped_and_does_not_allow_actual_denials(self):
        strict = PublicClient(SOURCE)
        strict._request = Mock(return_value=response(403, 'Forbidden'))
        with self.assertRaises(CrawlError):
            strict.allowed(SOURCE['url'])
        configured = PublicClient({**SOURCE, 'robots_unavailable_hosts': ['example.edu']})
        configured._request = Mock(return_value=response(403, 'Forbidden'))
        self.assertTrue(configured.allowed(SOURCE['url']))
        with self.assertRaises(CrawlError):
            configured.response(SOURCE['url'])
        challenged = PublicClient({**SOURCE, 'robots_unavailable_hosts': ['example.edu']})
        challenged._request = Mock(return_value=response(403, 'CAPTCHA verification'))
        with self.assertRaises(CrawlError):
            challenged.allowed(SOURCE['url'])


class DetailCacheReviewTests(unittest.TestCase):
    def verified(self, **changes):
        client = PublicClient(SOURCE)
        client.set_previous_jobs([], '2026-11-16T00:00:00+00:00')
        job = listing(**changes)
        self.assertFalse(client.reuse_detail(job))
        job.update(title='Lecturer in Philosophy', description='Verified philosophy teaching advertisement.',
                   match_text='Philosophy teaching', detail_complete=True, status='open')
        client.mark_detail_success(job)
        return job

    def test_cache_preserves_original_verification_time_and_expanded_detail_title(self):
        previous = self.verified()
        client = PublicClient(SOURCE)
        client.set_previous_jobs([previous], NOW)
        job = listing()
        self.assertTrue(client.reuse_detail(job))
        self.assertEqual(job['title'], 'Lecturer in Philosophy')
        self.assertEqual(job['detail_verified_at'], previous['detail_verified_at'])
        self.assertTrue(job['detail_from_cache'])
        self.assertEqual(client.detail_cache_hits, 1)
        self.assertEqual(client.count, 0)

    def test_changed_list_failed_refresh_missing_verification_and_weekly_expiry_refetch(self):
        old = self.verified()
        cases = [(old, listing(title='Senior Lecturer'), NOW),
                 ({**old, 'detail_refresh_failed': True}, listing(), NOW),
                 ({**old, 'detail_verified_at': ''}, listing(), NOW),
                 ({**old, 'detail_complete': False}, listing(), NOW),
                 ({**old, 'status': 'missing'}, listing(), NOW),
                 (old, listing(), '2026-11-23T00:00:00+00:00'),
                 (old, listing(), '2026-11-15T00:00:00+00:00')]
        for previous, job, now in cases:
            with self.subTest(previous=previous.get('detail_verified_at'), title=job['title'], now=now):
                client = PublicClient(SOURCE)
                client.set_previous_jobs([previous], now)
                self.assertFalse(client.reuse_detail(job))
                self.assertEqual(client.detail_refresh_candidates, 1)
                self.assertEqual(client.detail_fetches, 0)

    def test_near_deadline_always_revalidates(self):
        old = self.verified(deadline='2026-11-20', deadline_type='closing')
        client = PublicClient(SOURCE)
        client.set_previous_jobs([old], NOW)
        self.assertFalse(client.reuse_detail(listing(deadline='2026-11-20', deadline_type='closing')))

    def test_cache_can_shorten_but_not_extend_weekly_refresh(self):
        old = self.verified()
        client = PublicClient({**SOURCE, 'detail_cache_days': 100})
        client.set_previous_jobs([old], '2026-11-23T00:00:00+00:00')
        self.assertFalse(client.reuse_detail(listing()))

    def test_catalog_failure_does_not_unfinish_list_and_marks_refresh_failed(self):
        client = PublicClient(SOURCE)
        client.get = Mock(side_effect=CrawlError('HTTP 504'))
        batch = Batch(jobs=[listing()])
        read_details(SOURCE, client, batch, detail=lambda job: client.get(job['url']))
        self.assertTrue(batch.listing_complete)
        self.assertFalse(batch.complete)
        self.assertTrue(batch.jobs[0]['detail_refresh_failed'])
        self.assertFalse(batch.jobs[0]['detail_complete'])

    def test_catalog_uses_verified_cache_without_detail_request(self):
        old = self.verified()
        client = PublicClient(SOURCE)
        client.set_previous_jobs([old], NOW)
        client.get = Mock(side_effect=AssertionError('Unexpected detail request'))
        batch = Batch(jobs=[listing()])
        read_details(SOURCE, client, batch, detail=lambda job: client.get(job['url']))
        self.assertTrue(batch.complete)
        self.assertTrue(batch.listing_complete)
        self.assertTrue(batch.jobs[0]['detail_complete'])
        client.get.assert_not_called()

    def test_all_valid_cached_details_survive_an_early_challenge(self):
        old = self.verified()
        client = PublicClient(SOURCE)
        client.set_previous_jobs([old], NOW)
        new = record(SOURCE, 'New Lecturer', 'https://example.edu/job/456', '456', detail_complete=False)
        client.get = Mock(side_effect=CrawlError('challenge', stop_source=True))
        batch = Batch(jobs=[new, listing()])
        read_details(SOURCE, client, batch, detail=lambda job: client.get(job['url']))
        self.assertFalse(batch.complete)
        self.assertTrue(batch.jobs[1]['detail_complete'])
        self.assertTrue(batch.jobs[1]['detail_from_cache'])
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.detail_fetches, 1)

    def test_new_ads_take_priority_over_weekly_refresh(self):
        old = self.verified()
        client = PublicClient(SOURCE)
        client.set_previous_jobs([old], '2026-11-23T00:00:00+00:00')
        new = record(SOURCE, 'New Lecturer', 'https://example.edu/job/456', '456', detail_complete=False)
        queue = detail_jobs(client, [listing(), new])
        self.assertEqual(queue[0]['id'], new['id'])
        self.assertEqual(client.detail_refresh_candidates, 2)
        self.assertEqual(client.detail_fetches, 0)


if __name__ == '__main__':
    unittest.main()
