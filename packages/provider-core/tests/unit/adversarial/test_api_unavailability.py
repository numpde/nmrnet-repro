"""Operation-specific retry permission does not claim transience or a write outcome.

Problem fixture: API6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8,
security/problems.py::service_unavailable_problem_spec and problem_kinds.py.
Hello refresh and exact terminal replay have separate permission; no scientific
execution or other mutation acquires retry authority here.
"""

import asyncio
import errno
import json
import socket
import ssl
import unittest
from builtins import ExceptionGroup

import httpcore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ApiFailure, ProviderAPI
from provider_core.attempt_request import complete_body, fail_body
from provider_core.hello import AnalysisOffering
from provider_core.signing import SigningIdentity
from provider_core.transport import ExchangeFailed, HttpResponseHead
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.attempts import ATTEMPT_REF, retained_start
from tests.fakes.job_input import facts
from tests.fakes.uploads import UPLOAD_REF


def unavailable_problem(**changes):
    document = {
        "type": "urn:nmr-api:problem:service-unavailable",
        "title": "Service unavailable",
        "status": 503,
        "instance": "urn:nmr-api:request:test",
        "request_id": "request:test",
        "code": "service_unavailable",
        "detail": "PRIVATE_SERVER_DETAIL",
    }
    return json.dumps(document | changes).encode()


class ApiAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def failed_terminal(self, response, *, operation):
        api, transport, _ = await self.failed_read(reply(b"maintenance", status=503))
        transport.replies = iter((response,))
        with self.assertRaises(ApiFailure) as caught:
            if operation == "complete":
                await api.complete_attempt(
                    complete_body(ATTEMPT_REF, "fixture.result.v1", b"{}"), execution_attempt_ref=ATTEMPT_REF
                )
            else:
                await api.fail_attempt(
                    fail_body(ATTEMPT_REF, "fixture_failure", "Fixture stopped"), execution_attempt_ref=ATTEMPT_REF
                )
        return api, transport, caught.exception

    async def test_terminal_replay_permission_distinguishes_unverified_response_from_received_refusal(self):
        for operation in ("complete", "fail"):
            for response, expected in (
                (reply(b"maintenance", status=503), True),
                (reply(b"internal error", status=500), True),
                (reply(b"internal error", status=500, headers=response_headers(status=500, topology=b"web")), False),
                (
                    ExchangeFailed(
                        "Interrupted error body",
                        primary=httpcore.ReadTimeout(),
                        response_head=HttpResponseHead(500, response_headers(status=500)),
                    ),
                    True,
                ),
                (ExchangeFailed("No verified head", primary=httpcore.RemoteProtocolError("PRIVATE_WIRE")), True),
                (ExchangeFailed("Read timed out", primary=httpcore.ReadTimeout()), True),
                (reply(b"malformed receipt"), False),
                (reply(b"denied", status=401), False),
                (reply(b"conflict", status=409), False),
                (reply(b"maintenance", status=503, headers=response_headers(status=503, topology=b"web")), False),
                (
                    ExchangeFailed(
                        "Malformed body",
                        primary=httpcore.RemoteProtocolError(),
                        response_head=HttpResponseHead(200, response_headers()),
                    ),
                    False,
                ),
                (ExchangeFailed("Cleanup failed", primary=httpcore.RemoteProtocolError(), cleanup_failures=(OSError(),)), False),
                (
                    ExchangeFailed("Cancelled", primary=httpcore.RemoteProtocolError(), interruption=asyncio.CancelledError()),
                    False,
                ),
                (ExchangeFailed("Certificate rejected", primary=ssl.SSLCertVerificationError()), False),
            ):
                with self.subTest(operation=operation, response=response):
                    api, transport, failure = await self.failed_terminal(response, operation=operation)
                    self.assertIs(api.can_replay_terminal(failure), expected)
                    self.assertFalse(api.can_retry_read(failure))
                    self.assertFalse(api.can_refresh_hello(failure))
                    transport.retirement_confirmed = False
                    self.assertFalse(api.can_replay_terminal(failure))
                    await api.retire()
                    self.assertIs(api.can_replay_terminal(failure), expected)

    async def test_terminal_replay_requires_this_apis_current_failure_and_exact_unqueried_terminal_target(self):
        api, transport, old = await self.failed_terminal(reply(b"maintenance", status=503), operation="complete")
        operations = (
            ("GET", "/provider/v1/execution-attempts/complete", b""),
            ("POST", "/provider/v1/execution-attempts/start", b""),
            ("POST", "/provider/v1/hello", b""),
            ("POST", "/provider/v1/execution-attempts/complete", b"extra=1"),
            ("POST", "/provider/v1/execution-attempts/fail/", b""),
        )
        transport.replies = iter([reply(b"maintenance", status=503)] * len(operations))
        for method, path, query in operations:
            with self.subTest(method=method, path=path, query=query), self.assertRaises(ApiFailure) as caught:
                await api._exchange("untrusted label", method=method, path=path, query=query, maximum_bytes=4096, body=b"{}")
            self.assertFalse(api.can_replay_terminal(caught.exception))
            self.assertFalse(api.can_replay_terminal(old))

    async def failed_read(self, response):
        transport = Transport(response)
        api = ProviderAPI(
            SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()),
            transport,
            expected_topology="dev-local",
        )
        with self.assertRaises(ApiFailure) as caught:
            await api.job_input(facts(), analysis_kind_ref="nmr_prediction")
        self.assertEqual(len(transport.calls), 1)
        return api, transport, caught.exception

    async def test_safe_read_retry_handles_proxy_outages_without_admitting_their_body(self):
        for response, expected in (
            (reply(unavailable_problem(), status=503), True),
            (reply(b"PRIVATE_HTML", status=503), True),
            (reply(b"PRIVATE_HTML", status=502, headers=((b"content-type", b"text/html"),)), True),
            (reply(b"PRIVATE_HTML", status=504, headers=()), True),
            (reply(unavailable_problem(code="unknown"), status=503), True),
            (reply(unavailable_problem(request_id="request:other"), status=503), True),
            (reply(unavailable_problem(), status=500), False),
            (reply(unavailable_problem(), status=401), False),
            (reply(unavailable_problem(), status=503, headers=response_headers(status=503, topology=b"web")), False),
        ):
            with self.subTest(status=response.status, body=response.body):
                api, _, failure = await self.failed_read(response)
                self.assertIs(api.can_retry_read(failure), expected)
                self.assertNotIn("PRIVATE", str(failure))
                if response.body != unavailable_problem() or not expected:
                    self.assertIsNone(failure.problem_code)

    async def test_reviewed_io_evidence_is_distinct_from_configuration_and_protocol_errors(self):
        refused = httpcore.ConnectError("PRIVATE_IO")
        refused.__cause__ = OSError(errno.ECONNREFUSED, "PRIVATE_ADDRESS")
        certificate = httpcore.ConnectError("PRIVATE_TLS")
        certificate.__cause__ = ssl.SSLCertVerificationError("PRIVATE_CERTIFICATE")
        wrapped_group = httpcore.ConnectError("PRIVATE_GROUP")
        wrapped_group.__cause__ = OSError("All connection attempts failed")
        wrapped_group.__cause__.__cause__ = ExceptionGroup("connect", [ConnectionRefusedError(errno.ECONNREFUSED, "private")])
        for cause, expected in (
            (TimeoutError(), True),
            (httpcore.ConnectTimeout(), True),
            (httpcore.ReadTimeout(), True),
            (refused, True),
            (wrapped_group, True),
            (socket.gaierror(socket.EAI_AGAIN, "PRIVATE_DNS"), True),
            (socket.gaierror(socket.EAI_NONAME, "PRIVATE_DNS"), False),
            (certificate, False),
            (FileNotFoundError(errno.ENOENT, "PRIVATE_SOCKET"), False),
            (PermissionError(errno.EACCES, "PRIVATE_SOCKET"), False),
            (httpcore.ConnectError("unknown"), False),
            (httpcore.RemoteProtocolError("PRIVATE_FRAMING"), False),
            (ExchangeFailed("body limit"), False),
        ):
            with self.subTest(cause=type(cause).__name__):
                api, _, failure = await self.failed_read(ExchangeFailed("Exchange failed", primary=cause))
                self.assertIs(api.can_retry_read(failure), expected)

    async def test_retirement_and_late_original_evidence_control_the_decision(self):
        exchange = ExchangeFailed("Exchange failed", primary=TimeoutError())
        api, transport, failure = await self.failed_read(exchange)
        transport.retirement_confirmed = False
        self.assertFalse(api.can_retry_read(failure))
        await api.retire()
        self.assertTrue(api.can_retry_read(failure))
        exchange.primary = ssl.SSLCertVerificationError("PRIVATE_CERTIFICATE")
        self.assertFalse(api.can_retry_read(failure))
        self.assertFalse(api.can_retry_read(ApiFailure("other API", "unknown")))

    async def test_read_retry_never_authorizes_a_hello_post(self):
        api, transport, old_read = await self.failed_read(reply(b"maintenance", status=503))
        transport.replies = iter((reply(unavailable_problem(), status=503),))
        with self.assertRaises(ApiFailure) as caught:
            await api.publish_hello(
                provider_ref="provider:test",
                display_name="Test",
                description="Test",
                analysis_offerings=(AnalysisOffering("peaks_from_mol", "Test"),),
            )
        self.assertEqual(caught.exception.request_method, "POST")
        self.assertFalse(api.can_retry_read(caught.exception))
        self.assertTrue(api.can_refresh_hello(caught.exception))
        self.assertEqual(caught.exception.request_target, b"/provider/v1/hello")
        self.assertFalse(api.can_refresh_hello(old_read))
        transport.retirement_confirmed = False
        self.assertFalse(api.can_refresh_hello(caught.exception))
        await api.retire()
        self.assertTrue(api.can_refresh_hello(caught.exception))
        self.assertEqual(len(transport.calls), 2)

    async def test_hello_refresh_permission_excludes_other_mutations_and_queried_targets(self):
        api, transport, _ = await self.failed_read(reply(b"maintenance", status=503))
        operations = (
            lambda: api.read_grant("job:example", UPLOAD_REF),
            lambda: api.start_attempt(
                retained_start(), job_ref="job:example", analysis_kind_ref="nmr_predict", provider_ref="provider:example"
            ),
            lambda: api.progress_attempt(ATTEMPT_REF, phase="running", condition_code=None),
            lambda: api.complete_attempt(
                complete_body(ATTEMPT_REF, "fixture.result.v1", b"{}"), execution_attempt_ref=ATTEMPT_REF
            ),
            lambda: api.fail_attempt(
                fail_body(ATTEMPT_REF, "fixture_failure", "Fixture stopped"), execution_attempt_ref=ATTEMPT_REF
            ),
            lambda: api._exchange(
                "same English label", method="POST", path="/provider/v1/hello", query=b"extra=1", maximum_bytes=4096, body=b"{}"
            ),
        )
        transport.replies = iter([reply(unavailable_problem(), status=503)] * len(operations))
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ApiFailure) as caught:
                await operation()
            self.assertFalse(api.can_retry_read(caught.exception))
            self.assertFalse(api.can_refresh_hello(caught.exception))
        self.assertEqual(len(transport.calls), 1 + len(operations))

    async def test_partial_response_respects_observed_environment_before_read_retry(self):
        for head, expected in (
            (HttpResponseHead(200, response_headers()), True),
            (HttpResponseHead(200, response_headers(topology=b"web")), False),
            (HttpResponseHead(503, response_headers(status=503, request_id=b"request:test")), True),
            (HttpResponseHead(503, response_headers(status=503, topology=b"web")), False),
        ):
            with self.subTest(head=head):
                api, _, failure = await self.failed_read(
                    ExchangeFailed("Exchange failed", primary=TimeoutError(), response_head=head)
                )
                self.assertIs(api.can_retry_read(failure), expected)
