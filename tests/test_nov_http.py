"""A successful HTTP status is not evidence of usable rules or job content."""
import unittest
from unittest.mock import Mock

from monitor.http import PublicClient, CrawlError

SOURCE = {'url': 'https://example.edu/jobs', 'allowed_hosts': ['example.edu']}


class ResponseMeaningTests(unittest.TestCase):
    def test_html_robots_stays_denied_and_explains_http_200(self):
        client = PublicClient(SOURCE)
        client._request = Mock(return_value=Mock(status_code=200, text='<html><body>Verify access</body></html>', headers={}))
        with self.assertRaises(CrawlError) as caught:
            client.allowed(SOURCE['url'])
        self.assertTrue(caught.exception.stop_source)
        self.assertIn('非規則內容（HTTP 200）', str(caught.exception))
        with self.assertRaises(CrawlError):
            client.allowed(SOURCE['url'])
        self.assertEqual(client._request.call_count, 1)

    def test_empty_incapsula_shell_is_a_challenge(self):
        client = PublicClient(SOURCE)
        client.allowed = Mock(return_value=True)
        client._request = Mock(return_value=Mock(status_code=200, headers={}, text='<html><head><script src="/_Incapsula_Resource?token=private"></script></head><body>\n</body></html>'))
        with self.assertRaises(CrawlError) as caught:
            client.get(SOURCE['url'])
        self.assertTrue(caught.exception.stop_source)
        self.assertNotIn('private', str(caught.exception))

    def test_security_script_with_real_body_is_not_an_empty_shell(self):
        client = PublicClient(SOURCE)
        client.allowed = Mock(return_value=True)
        html = '<script src="/_Incapsula_Resource"></script><body>Lecturer advertisement and application information.</body>'
        client._request = Mock(return_value=Mock(status_code=200, headers={}, text=html))
        self.assertEqual(client.get(SOURCE['url']), html)
