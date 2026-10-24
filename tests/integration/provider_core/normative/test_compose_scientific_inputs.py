"""Historical producer identity remains separate from current scientific execution."""

import unittest
import copy
import math
import runpy
import subprocess
import sys
from pathlib import Path

from tests.integration.nmrnet.scientific_inputs import load_scientific_inputs
from tests.integration.nmrnet.reference_fixtures import PRODUCTION_SHA256, pinned_packet
from tests.integration.nmrnet.runtime_ports import require_download_credentials
from tests.fakes.uploads import UPLOAD_REF, grant


class ScientificInputTests(unittest.TestCase):
    def test_connected_fixture_ports_load_without_the_displaced_native_launch_harness(self):
        program = '''
import sys
sys.path[:0] = [sys.argv[1], "/consumer"]
from tests.integration.nmrnet import scientific_inputs, process_ports
from tests.fakes.api_tls import verify_signed_request
forbidden = {
    "tests.integration.nmrnet.native_lifecycle",
    "tests.integration.nmrnet.test_live_worker",
    "tests.integration.nmrnet.reference_service",
    "tests.integration.nmrnet.capacity_service",
    "tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle",
    "nmrnet_provider.allocation",
}
assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(Path(__file__).resolve().parents[4])],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_store_receiving_oracle_requires_its_bearer_and_refuses_other_credentials(self):
        target = f"GET /upload/v1/uploads/{UPLOAD_REF}/bytes HTTP/1.1\r\n".encode()
        authorized = b"Authorization: Bearer " + grant()["capability"].encode() + b"\r\n"
        require_download_credentials(target + authorized + b"\r\n", b"")
        for headers in (b"", b"Authorization: Bearer wrong\r\n", authorized + authorized,
                        authorized + b"Signature: leaked-api-signature\r\n",
                        authorized + b"Signature-Input: leaked-api-identity\r\n",
                        authorized + b"X-Leak: PRIVATE_MODEL_KEY\r\n"):
            with self.subTest(headers=headers), self.assertRaises(AssertionError):
                require_download_credentials(target + headers + b"\r\n", b"")

    def test_connected_checker_keeps_exact_original_population_and_rejects_one_ulp_change(self):
        _, manifest = load_scientific_inputs(Path("/fixture/scientific_inputs"), "references")
        production = pinned_packet("/fixture/prediction-cpu-float32.json", PRODUCTION_SHA256)
        fixtures = production["qualification"]["real_model_callable"]["fixture_observations"]
        result = {"geometries": [
            {"prepared_request": record["original_request"], "predictions": fixture["result"]["predictions"],
             "source": {"record_number": record["source_record_number"], "positions": record["source_positions"],
                        "sha256": manifest["source"]["sha256"]}}
            for record, fixture in zip(manifest["records"], fixtures, strict=True)
        ]}
        check = runpy.run_path("/consumer/compose_peer.py")["require_exported_result"]
        proof = check(result, "references", manifest)
        self.assertEqual((proof["complete_predictions"], proof["historical_positions"]), (137, 64))
        changed = copy.deepcopy(result)
        prediction = changed["geometries"][0]["predictions"][0]
        prediction["shift_ppm"] = math.nextafter(prediction["shift_ppm"], math.inf)
        with self.assertRaisesRegex(AssertionError, "Complete connected population differs"):
            check(changed, "references", manifest)

    def test_retained_inputs_match_original_reference_population_and_capacity_recipe(self):
        for case, counts in (("references", [42, 38, 40, 27]), ("capacity", [128, 128])):
            with self.subTest(case=case):
                source, manifest = load_scientific_inputs(Path("/fixture/scientific_inputs"), case)
                self.assertEqual([len(item["original_request"]["atoms"]) for item in manifest["records"]], counts)
                self.assertEqual(manifest["source"]["size"], len(source))
                self.assertEqual(manifest["producer"]["execution"]["image_id"],
                                 "sha256:cabb4fe3e36541b0fefe54ca7c16fb21f9a60112a1e3d47992d4aa18343ecd08")
