"""Explicit, host-scoped compatibility for a verified legacy university server.

Certificate and hostname validation remain mandatory. This exception only enables
client connections to an unpatched RFC 5746 server; it does not enable the broader
ALLOW_UNSAFE_LEGACY_RENEGOTIATION option or lower the TLS security level.
"""
from __future__ import annotations

import ssl

from requests.adapters import HTTPAdapter


# A source configuration cannot silently expand this compatibility exception.
LEGACY_SERVER_HOSTS = frozenset({'www.eduhk.hk'})


class LegacyServerAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        self.legacy_context = ssl.create_default_context()
        # OpenSSL's SSL_OP_LEGACY_SERVER_CONNECT is 0x4. Python builds that do not
        # expose the named constant still use the same documented OpenSSL flag.
        self.legacy_context.options |= getattr(ssl, 'OP_LEGACY_SERVER_CONNECT', 0x4)
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        kwargs['ssl_context'] = self.legacy_context
        return super().init_poolmanager(connections, maxsize, block=block, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        # The target connection uses the exception; the proxy's own TLS context
        # is left unchanged. Normal certificate checks still run in requests.
        kwargs['ssl_context'] = self.legacy_context
        return super().proxy_manager_for(proxy, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        if verify is False:
            raise ValueError('Legacy TLS compatibility still requires certificate validation.')
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        # requests 2.32+ supplies its own preloaded context for verify=True;
        # explicitly retain this adapter's narrowly configured client context.
        pool_kwargs['ssl_context'] = self.legacy_context
        pool_kwargs['cert_reqs'] = 'CERT_REQUIRED'
        return host_params, pool_kwargs


def configure_host_tls(session, source):
    requested = set(source.get('legacy_tls_hosts', []))
    allowed = set(source.get('allowed_hosts', []))
    if not requested <= LEGACY_SERVER_HOSTS or not requested <= allowed:
        raise ValueError('Unapproved host-specific legacy TLS configuration.')
    for host in requested:
        # Including the trailing slash avoids matching a lookalike host suffix.
        session.mount(f'https://{host}/', LegacyServerAdapter())
