"""Received source spelling may differ while the scientific oracle stays exact."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from api_prediction import (
    Client,
    compare_science,
    compare_scientific_documents,
    compare_sdf_scientific_documents,
    compare_smiles_scientific_documents,
    compare_upload_scientific_documents,
    preflight_sdf_zip,
    receive,
    submit,
)
from prediction_test_ports import (
    UploadResponse,
    clocked_client,
    clocked_upload_client,
    generated_documents,
    generated_multiple_documents,
    scientific_documents,
    synthetic_signer,
    upload_client,
    uploaded_documents,
    uploaded_sdf_documents,
)


class ApiReadSigningTests(unittest.TestCase):
    def client(self, output, replies):
        client = Client.__new__(Client)
        client.output, client.count = output, 0
        client.credential = output / "synthetic-key.json"
        client.identity = {"authority": "api.example", "topology": "dev-local"}
        client._exchange = Mock(side_effect=replies)
        return client

    def refusal(self, *, code="authentication_window_closed", topology="dev-local"):
        return UploadResponse(401, topology=topology), json.dumps({
            "type": "urn:nmr-api:problem:authentication-failed", "status": 401, "code": code,
        }).encode()

    def test_retention_window_refusal_refreshes_only_the_get_signature_and_keeps_both_replies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = self.client(root, [self.refusal(), (UploadResponse(200, topology="dev-local"), b'{"ok":true}')])
            with patch("api_prediction.sign_request", side_effect=synthetic_signer) as signer:
                self.assertEqual(client.request("readback", "GET", "/v1/jobs/job:test/analysis-results"), {"ok": True})
            self.assertEqual(signer.call_count, 2)
            self.assertEqual([call.args[1] for call in client._exchange.call_args_list], ["GET", "GET"])
            self.assertEqual(json.loads((root / "001-readback.response.json").read_bytes())["code"], "authentication_window_closed")
            self.assertEqual(json.loads((root / "002-readback.response.json").read_bytes()), {"ok": True})

    def test_repeated_window_refusal_is_bounded_and_other_refusals_or_mutations_are_not_retried(self):
        for method, code, topology, expected in (
            ("GET", "authentication_window_closed", "dev-local", 3),
            ("GET", "authentication_nonce_reused", "dev-local", 1),
            ("GET", "authentication_window_closed", "other", 1),
            ("POST", "authentication_window_closed", "dev-local", 1),
        ):
            with self.subTest(method=method, code=code, topology=topology), tempfile.TemporaryDirectory() as temporary:
                client = self.client(Path(temporary), [self.refusal(code=code, topology=topology)] * 3)
                with patch("api_prediction.sign_request", side_effect=synthetic_signer) as signer, self.assertRaises(AssertionError):
                    client.request("readback", method, "/v1/jobs/job:test/analysis-results")
                self.assertEqual(signer.call_count, expected)


class ApiPredictionTests(unittest.TestCase):
    def test_corrected_fallback_delivery_keeps_exact_science_and_actual_bounded_routes(self):
        actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
        fallback = {"configuration_id": "fallback", "model": "fallback-model", "configuration_sha256": "d" * 64}
        interpreter["endpoints"].append(fallback)
        observation = actual["interpretation"]
        observation.update(turns_started=3, corrections=1, operations=["prepare_pasted", "prepare_pasted"],
                           routes=[{"endpoint": interpreter["endpoints"][0], "turns_started": 1,
                                    "failure": "exchange_failed", "http_status": 503},
                                   {"endpoint": fallback, "turns_started": 2}])
        before = copy.deepcopy(actual)
        compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter,
                                     prediction_image_id=retained["execution"]["image_id"])
        self.assertEqual(actual, before)
        for change in ("budget", "route", "count"):
            changed = copy.deepcopy(actual)
            if change == "budget":
                changed["interpretation"]["turns_started"] = 7
            elif change == "route":
                changed["interpretation"]["routes"][1]["endpoint"]["model"] = "unconfigured"
            else:
                changed["interpretation"]["routes"][1]["turns_started"] = 1
            with self.subTest(change=change), self.assertRaisesRegex(AssertionError, "Selected interpretation"):
                compare_scientific_documents(changed, retained, receipt, metadata, interpreter=interpreter,
                                             prediction_image_id=retained["execution"]["image_id"])

    def test_current_image_pin_changes_no_historical_scientific_expectation(self):
        actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
        original = copy.deepcopy(retained)
        current = "sha256:" + "9" * 64
        actual["execution"]["image_id"] = current
        compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter,
                                     prediction_image_id=current)
        self.assertEqual(retained, original)
        with self.assertRaisesRegex(AssertionError, "admitted current deployment"):
            compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter,
                                         prediction_image_id="sha256:" + "8" * 64)
        actual["geometries"][0]["predictions"][0]["shift_ppm"] += 0.000001
        with self.assertRaisesRegex(AssertionError, "predictions"):
            compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter,
                                         prediction_image_id=current)

    def test_explicit_sdf_resume_renews_same_upload_then_opens_one_job_without_another_reservation(self):
        _actual, retained, _receipt, _metadata, _interpreter, _upload = uploaded_sdf_documents()
        with patch("api_prediction._retained_sdf_science", return_value=retained):
            source = preflight_sdf_zip()
        original = {
            "schema_id": "nmr.upload.create.response.v1",
            "upload_ref": "upload:sha256:" + "d" * 64,
            "project_ref": "project:user-demo",
            "creator_ref": "principal:user:demo",
            "byte_length": len(source),
            "created_at": "2026-10-21T00:00:00Z",
            "operation_ref": "original-operation",
            "request_fingerprint": "sha256:" + "f" * 64,
            "replayed": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "original.json"
            path.write_text(json.dumps(original))
            client, calls, _source, _canary = upload_client(root, fault=None, source=source, existing=original)
            with (
                patch("api_prediction._retained_sdf_science", return_value=retained),
                patch("api_prediction.sign_request", side_effect=synthetic_signer),
            ):
                receipt = submit(
                    client, {"specification": "Both SDF records, angstrom."}, case_kind="sdf-zip", upload_receipt=path
                )
            self.assertEqual(receipt["job_ref"], "job:upload-new")
            self.assertFalse(any(method == "POST" and target == "/v1/uploads" for method, target, _body, _headers in calls))
            self.assertEqual(sum(method == "POST" and target == "/v1/jobs" for method, target, _body, _headers in calls), 1)
            self.assertEqual(sum(target.startswith("/upload/v1/") for _method, target, _body, _headers in calls), 1)
            self.assertEqual(json.loads((root / "upload-created.json").read_bytes()), original)
            self.assertEqual(json.loads((root / "upload.json").read_bytes())["upload_ref"], original["upload_ref"])

    def test_sdf_zip_publishes_one_pinned_member_then_attaches_and_opens_one_job(self):
        actual, retained, receipt, metadata, interpreter, upload = uploaded_sdf_documents()
        compare_sdf_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter, prediction_image_id="sha256:" + "c" * 64, upload=upload)
        with patch("api_prediction._retained_sdf_science", return_value=retained):
            archive = preflight_sdf_zip()
            self.assertEqual(preflight_sdf_zip(), archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            self.assertEqual(source.namelist(), ["butane_conformers.sdf"])
            self.assertEqual(source.read("butane_conformers.sdf"), Path("/fixture/butane_conformers.sdf").read_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            client, calls, _source, canary = upload_client(Path(temporary), fault=None, source=archive)
            with (
                patch("api_prediction._retained_sdf_science", return_value=retained),
                patch("api_prediction.sign_request", side_effect=synthetic_signer),
            ):
                receipt = submit(client, {"specification": "Read both SDF records from the ZIP; angstrom."}, case_kind="sdf-zip")
            self.assertEqual(receipt["job_ref"], "job:upload-new")
            self.assertEqual(len([call for call in calls if call[0] != "GET"]), 8)
            reservation = json.loads(
                next(body for method, path, body, _headers in calls if method == "POST" and path == "/v1/uploads")
            )
            self.assertEqual(
                (reservation["filename"], reservation["media_type"], reservation["byte_length"]),
                ("butane_conformers.sdf.zip", "application/zip", len(archive)),
            )
            transfers = [call for call in calls if call[1].startswith("/upload/v1/")]
            self.assertEqual(len(transfers), 1)
            self.assertEqual(transfers[0][2], archive)
            self.assertEqual([call[1].rsplit("/", 1)[-1] for call in calls[-3:]], ["uploads", "providers", "state"])
            self.assertFalse(any(canary.encode() in item.read_bytes() for item in client.output.iterdir()))

    def test_qualification_accepts_explicit_seventeen_digit_output_with_the_existing_oracle(self):
        actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
        self.assertEqual(actual["output"], {"shift_significant_digits": 17, "verbosity": "max"})
        self.assertNotIn("output", retained)
        with patch("api_prediction._retained_science", return_value=retained):
            compare_science(json.dumps(actual).encode(), receipt, metadata, interpreter=interpreter, prediction_image_id="sha256:" + "c" * 64, case_kind="pasted")

    def test_upload_publication_and_late_success_share_the_profile_without_another_mutation(self):
        _actual, retained, _receipt, _metadata, _interpreter, _upload, _source, _expected = uploaded_documents()
        with tempfile.TemporaryDirectory() as temporary:
            client, clock, calls = clocked_upload_client(Path(temporary), ready_at=970)
            with (
                patch("api_prediction._retained_science", return_value=retained),
                patch("api_prediction.sign_request", side_effect=synthetic_signer),
                patch("api_prediction.time.monotonic", clock.monotonic),
                patch("api_prediction.time.sleep", clock.sleep),
                patch("api_prediction.compare_science") as compare,
            ):
                receipt = submit(client, {"specification": "Use attached XYZ, angstrom, record 1."}, case_kind="upload")
                self.assertEqual(clock.seconds, 90)
                upload_bytes, job_bytes = (
                    (client.output / "upload.json").read_bytes(),
                    (client.output / "created.json").read_bytes(),
                )
                mutations = [call for call in calls if call[0] != "GET"]
                receive(client, receipt, readback_seconds=900, case_kind="upload", prediction_image_id="sha256:" + "c" * 64)
            self.assertEqual(clock.seconds, 970)
            self.assertGreater(clock.seconds, 960)
            self.assertLess(clock.seconds, 990)
            self.assertLess(clock.seconds, 1020)
            self.assertEqual([call for call in calls if call[0] != "GET"], mutations)
            self.assertEqual(len(mutations), 8)
            self.assertEqual((client.output / "upload.json").read_bytes(), upload_bytes)
            self.assertEqual((client.output / "created.json").read_bytes(), job_bytes)
            compare.assert_called_once()

    def test_upload_publishes_once_finalizes_attaches_closed_job_then_opens_without_retaining_bearer(self):
        _actual, retained, _receipt, _metadata, _interpreter, _upload, _source, _expected = uploaded_documents()
        with tempfile.TemporaryDirectory() as temporary:
            client, calls, source, canary = upload_client(Path(temporary), fault=None)
            with (
                patch("api_prediction._retained_science", return_value=retained),
                patch("api_prediction.sign_request", side_effect=synthetic_signer) as signer,
            ):
                receipt = submit(
                    client, {"specification": "Use attached atom_order.xyz, XYZ, angstrom, record 1."}, case_kind="upload"
                )
            self.assertEqual(receipt["job_ref"], "job:upload-new")
            self.assertEqual(len(calls), 9)
            self.assertEqual(signer.call_count, 8)
            byte_requests = [call for call in calls if call[1].startswith("/upload/v1/")]
            self.assertEqual(len(byte_requests), 1)
            method, _path, sent, headers = byte_requests[0]
            self.assertEqual((method, sent), ("PUT", source))
            self.assertEqual(
                headers,
                {
                    "Authorization": "Bearer " + canary,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(source)),
                },
            )
            for _method, path, _body, headers in calls:
                if not path.startswith("/upload/v1/"):
                    self.assertEqual(headers, {"Signature": "synthetic-api-signature"})
            self.assertEqual([call[1].rsplit("/", 1)[-1] for call in calls[-3:]], ["uploads", "providers", "state"])
            self.assertFalse(any(canary.encode() in path.read_bytes() for path in client.output.iterdir()))

    def test_uploaded_xyz_uses_authored_angstrom_values_and_both_exact_binary32_oracles(self):
        actual, retained, receipt, metadata, interpreter, upload, source, expected = uploaded_documents()
        observed = copy.deepcopy(actual)
        compare_upload_scientific_documents(
            actual, retained, receipt, metadata, interpreter=interpreter, prediction_image_id="sha256:" + "c" * 64, upload=upload, source=source, expected=expected
        )
        self.assertEqual(actual, observed)

    def test_ccl_one_conformer_checks_observed_generation_without_a_numerical_golden(self):
        actual, retained, receipt, metadata, interpreter, expected = generated_documents()
        self.assertEqual(expected["requested_conformers"], 1)
        observed = copy.deepcopy(actual)
        compare_smiles_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter, prediction_image_id="sha256:" + "c" * 64, expected=expected)
        self.assertEqual(actual, observed)
        self.assertNotEqual(actual["geometries"][0]["prepared_request"], retained["geometries"][0]["prepared_request"])
        self.assertNotEqual(actual["geometries"][0]["predictions"], retained["geometries"][0]["predictions"])

    def test_three_cccc_conformers_check_each_observation_without_requiring_distinct_numerical_values(self):
        actual, retained, receipt, metadata, interpreter, expected = generated_multiple_documents()
        observed = copy.deepcopy(actual)
        self.assertEqual(expected["requested_conformers"], 3)
        self.assertEqual([geometry["source"]["conformer_id"] for geometry in actual["geometries"]], [2, 7, 11])
        self.assertEqual(actual["geometries"][0]["prepared_request"], actual["geometries"][1]["prepared_request"])
        self.assertEqual(actual["geometries"][0]["predictions"], actual["geometries"][2]["predictions"])
        compare_smiles_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter, prediction_image_id="sha256:" + "c" * 64, expected=expected)
        self.assertEqual(actual, observed)

    def test_multiple_mode_selects_the_authored_three_conformer_case_and_describes_butane(self):
        actual, retained, receipt, metadata, interpreter, _expected = generated_multiple_documents()
        with patch("api_prediction._retained_science", return_value=retained):
            compare_science(json.dumps(actual).encode(), receipt, metadata, interpreter=interpreter, prediction_image_id="sha256:" + "c" * 64, case_kind="smiles-multiple")
        with tempfile.TemporaryDirectory() as temporary:
            client, _clock, calls = clocked_client(Path(temporary), ready_at=0)
            submit(client, {"specification": "Generate three CCCC conformers separately."}, case_kind="smiles-multiple")
            create = next(call for call in calls if call[0] == "create")
            self.assertEqual(json.loads(create[4])["description"], "NMRNet butane; real interpreter qualification")

    def test_real_whitespace_or_xyz_header_changes_only_received_source_evidence(self):
        for source_format in ("table", "xyz"):
            with self.subTest(source_format=source_format):
                actual, retained, receipt, metadata, interpreter, original = scientific_documents()
                received = (
                    "\n\n" + original.replace(" ", "   ") + "\n"
                    if source_format == "table"
                    else "5\nreceived XYZ comment\n" + original
                )
                source = actual["geometries"][0]["source"]
                source.update(
                    format=source_format,
                    sha256=hashlib.sha256(received.encode()).hexdigest(),
                    byte_length=len(received.encode()),
                    positions=[3, 4, 5, 6, 7],
                )
                if source_format == "xyz":
                    actual["geometries"][0]["preparation"]["method"] = "xyz"
                observed = copy.deepcopy(actual)
                compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter, prediction_image_id="sha256:" + "c" * 64)
                self.assertEqual(actual, observed)

    def test_scripted_comparison_still_accepts_only_its_exact_source_and_science(self):
        actual, retained, receipt, metadata, _interpreter, _source = scientific_documents()
        compare_scientific_documents(actual, retained, receipt, metadata, interpreter=None, prediction_image_id="sha256:" + "c" * 64)

    def test_delayed_result_crosses_old_reader_and_process_limits_without_another_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, clock, calls = clocked_client(Path(temporary), ready_at=601)
            receipt = submit(client, {"specification": "Fixed supplied coordinates in nm"}, case_kind="pasted")
            created = (client.output / "created.json").read_bytes()
            with (
                patch("api_prediction.time.monotonic", clock.monotonic),
                patch("api_prediction.time.sleep", clock.sleep),
                patch("api_prediction.compare_science") as compare,
            ):
                receive(client, receipt, readback_seconds=900, case_kind="pasted", prediction_image_id="sha256:" + "c" * 64)
            self.assertEqual(clock.seconds, 601)
            self.assertEqual((client.output / "created.json").read_bytes(), created)
            self.assertEqual(json.loads(created), receipt)
            self.assertTrue((client.output / "scientific-result.json").exists())
            self.assertEqual([method for _label, method, *_rest in calls if method != "GET"], ["POST", "PUT", "PUT"])
            compare.assert_called_once()
