"""API-marked local TLS replies; model/store peers retain their generic owner."""

from tests.fakes.api import response_headers
from tests.fakes.tls_peer import TlsPeer


def api_tls_peer(mode, **options):
    if "response_headers" not in options:
        options["response_headers"] = response_headers()
    return TlsPeer(mode, **options)
