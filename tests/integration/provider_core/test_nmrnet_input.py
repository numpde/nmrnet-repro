"""Verified bytes reach the existing scientific admission without a second parser."""

import hashlib
import json
import unittest
from unittest.mock import patch

from tests.fakes.job_input import facts, response

from nmrnet_inference.predict import parse_request
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.prepared_input import DEFAULT_OUTPUT, OutputSettings
from nmrnet_provider.submission import prepare_direct_job
from provider_core.job_input import JobInputRejected, verify_job_input


class NMRNetInputTests(unittest.TestCase):
    def test_installed_verifier_supplies_exact_original_coordinates(self):
        raw = b' {"atoms":["H","C"],"coordinates":[[1.09,0,0],[0,0,0]],"coordinate_unit":"angstrom"}\n'
        verified = verify_job_input(response(raw), feed=facts(raw))
        prepared = prepare_direct_job(verified)
        geometry = prepared.geometries[0]
        self.assertEqual(geometry.request_bytes, raw)
        self.assertEqual(verified.identity.input_fingerprint, "sha256:" + geometry.source_sha256)
        self.assertEqual(geometry.source_positions, (0, 1))
        self.assertEqual(prepared.output, DEFAULT_OUTPUT)

    def test_output_fragment_is_separated_before_native_admission_with_original_source_identity(self):
        scientific = b'{"atoms":["H","C"],"coordinates":[[1.09,0,0],[0,0,0]],"coordinate_unit":"angstrom"}'
        for verbosity in ("low", "medium", "max"):
            with self.subTest(verbosity=verbosity):
                fragment = {"verbosity": verbosity, "shift_significant_digits": 5}
                raw = scientific[:-1] + b',"output":' + json.dumps(fragment).encode() + b"}"
                verified = verify_job_input(response(raw), feed=facts(raw))
                prepared = prepare_direct_job(verified)
                geometry = prepared.geometries[0]
                self.assertEqual(prepared.output, OutputSettings.from_json(fragment))
                self.assertEqual(geometry.request_bytes, scientific)
                self.assertEqual(geometry.source_sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(verified.identity.input_fingerprint, "sha256:" + geometry.source_sha256)
                self.assertNotEqual(geometry.source_sha256, hashlib.sha256(geometry.request_bytes).hexdigest())
                self.assertEqual(set(parse_request(geometry.request_bytes)), {"atoms", "coordinates", "coordinate_unit"})
                with self.assertRaisesRegex(ValueError, "only coordinate_unit"):
                    parse_request(raw)

    def test_duplicate_source_or_output_keys_are_refused_before_geometry_preparation(self):
        prefix = b'{"atoms":["H"],"coordinates":[[0,0,0]],"coordinate_unit":"angstrom",'
        for suffix in (
            b'"atoms":["C"]}',
            b'"coordinates":[[1,2,3]]}',
            b'"output":{},"output":{"verbosity":"low"}}',
            b'"output":{"verbosity":"low","verbosity":"max"}}',
            b'"output":{"shift_significant_digits":4,"shift_significant_digits":17}}',
        ):
            raw = prefix + suffix
            with self.subTest(suffix=suffix), patch("nmrnet_provider.submission.prepare_json") as prepare:
                with self.assertRaisesRegex(InputRejected, "duplicate key"):
                    prepare_direct_job(verify_job_input(response(raw), feed=facts(raw)))
                prepare.assert_not_called()

    def test_invalid_output_fragments_never_reach_geometry_preparation(self):
        for fragment in (
            None,
            "low",
            [],
            {"verbosity": "compact"},
            {"verbosity": "full"},
            {"verbosity": None},
            {"shift_significant_digits": True},
            {"unknown": 1},
        ):
            raw = json.dumps(
                {"atoms": ["H"], "coordinates": [[0, 0, 0]], "coordinate_unit": "angstrom", "output": fragment}
            ).encode()
            with self.subTest(fragment=fragment), patch("nmrnet_provider.submission.prepare_json") as prepare:
                with self.assertRaises(InputRejected):
                    prepare_direct_job(verify_job_input(response(raw), feed=facts(raw)))
                prepare.assert_not_called()

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
