"""Verified bytes reach the existing scientific admission without a second parser."""

import unittest
from unittest.mock import patch

from tests.fakes.job_input import facts, response

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.submission import prepare_direct_job
from provider_core.job_input import JobInputRejected, verify_job_input


class NMRNetInputTests(unittest.TestCase):
    def test_installed_verifier_supplies_exact_original_coordinates(self):
        raw = b' {"atoms":["H","C"],"coordinates":[[1.09,0,0],[0,0,0]],"coordinate_unit":"angstrom"}\n'
        verified = verify_job_input(response(raw), feed=facts(raw))
        prepared = prepare_direct_job(verified)
        self.assertEqual(prepared.request_bytes, raw)
        self.assertEqual(verified.identity.input_fingerprint, "sha256:" + prepared.source_sha256)
        self.assertEqual(prepared.source_positions, (0, 1))

    def test_rejected_verification_cannot_reach_preparation(self):
        with patch("nmrnet_provider.submission.prepare_json") as prepare:
            with self.assertRaises(JobInputRejected):
                verified = verify_job_input(response(job_ref="job:substituted"), feed=facts())
                prepare_direct_job(verified)
            prepare.assert_not_called()

    def test_invalid_geometry_is_not_reinterpreted(self):
        raw = b'{"atoms":'
        with self.assertRaises(InputRejected):
            prepare_direct_job(verify_job_input(response(raw), feed=facts(raw)))
