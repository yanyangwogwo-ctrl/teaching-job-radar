import ssl
import unittest

import requests

from monitor.tls import LegacyServerAdapter, configure_host_tls


class LegacyTlsTests(unittest.TestCase):
    def test_exception_is_exact_host_and_keeps_certificate_verification(self):
        session = requests.Session()
        configure_host_tls(session, {'allowed_hosts': ['www.eduhk.hk'], 'legacy_tls_hosts': ['www.eduhk.hk']})
        adapter = session.get_adapter('https://www.eduhk.hk/en/current-openings')
        self.assertIsInstance(adapter, LegacyServerAdapter)
        self.assertNotIsInstance(session.get_adapter('https://www.eduhk.hk.example.org/'), LegacyServerAdapter)
        self.assertNotIsInstance(session.get_adapter('https://example.org/'), LegacyServerAdapter)
        context = adapter.legacy_context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertTrue(context.options & getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4))
        request = requests.Request('GET', 'https://www.eduhk.hk/robots.txt').prepare()
        _, pool = adapter.build_connection_pool_key_attributes(request, True)
        self.assertIs(pool['ssl_context'], context)
        self.assertEqual(pool['cert_reqs'], 'CERT_REQUIRED')
        with self.assertRaises(ValueError):
            adapter.build_connection_pool_key_attributes(request, False)

    def test_exception_cannot_be_expanded_through_configuration(self):
        for source in (
            {'allowed_hosts': ['other.example'], 'legacy_tls_hosts': ['other.example']},
            {'allowed_hosts': [], 'legacy_tls_hosts': ['www.eduhk.hk']},
        ):
            with self.assertRaises(ValueError):
                configure_host_tls(requests.Session(), source)


if __name__ == '__main__':
    unittest.main()
