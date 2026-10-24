"""Core operator output preserves owned causes without provider/scientific imports."""

import errno
import json
import os
import unittest
from contextlib import redirect_stderr

from provider_core.api import ApiFailure
from provider_core.delivery_worker import DeliveryWorkerFailed
from provider_core.diagnostics import failure_evidence, write_stderr
from provider_core.transport import HttpResponse


class DeliveryDiagnosticsTests(unittest.TestCase):
    def test_api_effect_and_independent_cleanup_reach_the_core_renderer_without_payloads(self):
        api = ApiFailure("publish Attempt failure", "received API refusal", response=HttpResponse(
            429, ((b"x-request-id", b"request:delivery"), (b"authorization", b"Bearer PRIVATE_HEADER")), b"PRIVATE_BODY"
        ))
        api.attempt_mutation_may_have_committed = True
        cleanup = OSError(errno.EIO, "PRIVATE_NATIVE")
        failure = DeliveryWorkerFailed(cause=api, api_failure=api, cleanup_failures=(cleanup,), api_receipt_received=False)
        report = failure_evidence(failure, secrets=())
        rendered = json.dumps(report)
        self.assertIn('"status": 429', rendered)
        self.assertIn("request:delivery", rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertIn('"attempt_mutation_may_have_committed": true', rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_unknown_exception_text_and_credential_fields_stay_out_of_operator_output(self):
        unknown = ValueError("PRIVATE_BODY token=sk-PRIVATE_VALUE")
        failure = ApiFailure("publish Attempt failure", 'secret="escaped\\\"PRIVATE_SECRET"; header Bearer PRIVATE_AUTH', cause=unknown)
        rendered = json.dumps(failure_evidence(failure, secrets=("PRIVATE_SECRET",)))
        self.assertNotIn("PRIVATE", rendered)
        self.assertIn("ValueError", rendered)

    def test_full_stderr_pipe_refuses_without_blocking_or_changing_descriptor_policy(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(writer, False)
            while True:
                try:
                    os.write(writer, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(writer, True)
            with os.fdopen(writer, "w", closefd=False) as output, redirect_stderr(output):
                with self.assertRaises(BlockingIOError):
                    write_stderr(b"owned diagnostic\n")
            self.assertTrue(os.get_blocking(writer))
        finally:
            os.close(writer)
            os.close(reader)
