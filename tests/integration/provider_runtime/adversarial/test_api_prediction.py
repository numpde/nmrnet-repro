"""Corrupted science and extra interpretation exchanges fail qualification."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import math
import struct
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api_prediction import (
    Client,
    compare_science,
    compare_scientific_documents,
    compare_smiles_scientific_documents,
    compare_upload_scientific_documents,
    main,
    receive,
    submit,
)
from prediction_test_ports import (
    UploadResponse,
    clocked_client,
    clocked_upload_client,
    generated_documents,
    scientific_documents,
    synthetic_signer,
    upload_client,
    uploaded_documents,
)


class ApiPredictionAdversarialTests(unittest.TestCase):
    def test_upload_publication_then_readback_expiry_preserves_both_receipts_without_resubmission(self):
        _actual, retained, _receipt, _metadata, _interpreter, _upload, _source, _expected = uploaded_documents()
        with tempfile.TemporaryDirectory() as temporary:
            client, clock, calls = clocked_upload_client(Path(temporary), ready_at=2000)
            with (
                patch("api_prediction._retained_science", return_value=retained),
                patch("api_prediction.sign_request", side_effect=synthetic_signer),
                patch("api_prediction.time.monotonic", clock.monotonic),
                patch("api_prediction.time.sleep", clock.sleep),
            ):
                receipt = submit(client, {"specification": "Use attached XYZ, angstrom, record 1."}, case_kind="upload")
                upload_bytes, job_bytes = (
                    (client.output / "upload.json").read_bytes(),
                    (client.output / "created.json").read_bytes(),
                )
                mutations = [call for call in calls if call[0] != "GET"]
                with self.assertRaisesRegex(TimeoutError, "no mutation was retried"):
                    receive(client, receipt, readback_seconds=900, case_kind="upload")
            self.assertEqual(clock.seconds, 990)
            self.assertEqual([call for call in calls if call[0] != "GET"], mutations)
            self.assertEqual(len(mutations), 8)
            self.assertEqual((client.output / "upload.json").read_bytes(), upload_bytes)
            self.assertEqual((client.output / "created.json").read_bytes(), job_bytes)

    def test_put_acknowledgement_is_retained_when_close_fails_and_no_downstream_mutation_runs(self):
        _actual, retained, _receipt, _metadata, _interpreter, _upload, _source, _expected = uploaded_documents()
        with tempfile.TemporaryDirectory() as temporary:
            client, calls, _source, canary = upload_client(Path(temporary), fault=None)
            finite_exchange = client._exchange
            client.trust = Mock()
            response = UploadResponse(201, topology=client.identity["topology"])
            response.read = Mock(return_value=canary.encode())
            connection = Mock()
            connection.getresponse.return_value = response
            connection.close.side_effect = OSError(5, canary)

            def receive_put(prefix, method, path, query, body, headers, maximum_bytes):
                if path.startswith("/upload/v1/"):
                    calls.append((method, path, body, headers))
                    return Client._exchange(client, prefix, method, path, query, body, headers, maximum_bytes)
                return finite_exchange(prefix, method, path, query, body, headers, maximum_bytes)

            client._exchange = receive_put
            with (
                patch("api_prediction._retained_science", return_value=retained),
                patch("api_prediction.sign_request", side_effect=synthetic_signer),
                patch("api_prediction.http.client.HTTPSConnection", return_value=connection) as constructor,
                patch("api_prediction.connect_bound") as bound,
                self.assertRaises(RuntimeError) as caught,
            ):
                submit(client, {"specification": "Use attached XYZ."}, case_kind="upload")
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertIn("connection close: OSError, errno=5", rendered)
            self.assertNotIn(canary, rendered)
            self.assertFalse(any(canary.encode() in path.read_bytes() for path in client.output.iterdir()))
            self.assertEqual(len([call for call in calls if call[1].startswith("/upload/v1/")]), 1)
            self.assertFalse(any(path.endswith("/finalize") or path.startswith("/v1/jobs") for _method, path, *_rest in calls))
            head = json.loads(next(client.output.glob("*-upload-bytes.head.json")).read_bytes())
            self.assertEqual((head["status"], head["request_id"]), (201, "synthetic-request-id"))
            constructor.assert_called_once_with("api.example", 9443, timeout=10, context=client.trust)
            bound.assert_called_once_with("192.0.2.1", 9443, "fixture0")
            self.assertEqual(client.trust.wrap_socket.call_args.kwargs["server_hostname"], "api.example")
            connection.close.assert_called_once()

    def test_carrier_keeps_primary_and_close_failures_including_interruption_without_secret_text(self):
        for primary in (
            OSError(104, "synthetic-publication-secret-canary"),
            KeyboardInterrupt("synthetic-publication-secret-canary"),
        ):
            with self.subTest(primary=type(primary).__name__), tempfile.TemporaryDirectory() as temporary:
                client, _calls, source, canary = upload_client(Path(temporary), fault=None)
                del client._exchange
                client.trust = Mock()
                response = UploadResponse(200, topology=client.identity["topology"])
                response.read = Mock(side_effect=primary)
                connection = Mock()
                connection.getresponse.return_value = response
                connection.close.side_effect = OSError(5, canary)
                with (
                    patch("api_prediction.http.client.HTTPSConnection", return_value=connection),
                    patch("api_prediction.connect_bound"),
                    patch("api_prediction.sign_request", side_effect=synthetic_signer),
                    self.assertRaises(RuntimeError) as caught,
                ):
                    client.publication_grant({"upload_ref": "upload:sha256:" + "d" * 64, "byte_length": len(source)})
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertIn("response body: " + type(primary).__name__, rendered)
                self.assertIn("connection close: OSError, errno=5", rendered)
                self.assertNotIn(canary, rendered)
                connection.close.assert_called_once()
                self.assertFalse(any(canary.encode() in path.read_bytes() for path in client.output.iterdir()))
                head = json.loads(next(client.output.glob("*.head.json")).read_bytes())
                self.assertEqual((head["status"], head["request_id"]), (200, "synthetic-request-id"))

    def test_bound_raw_socket_is_owned_before_timeout_or_tls_setup_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, _calls, _source, canary = upload_client(Path(temporary), fault=None)
            del client._exchange
            outbound, connection = Mock(), Mock()
            outbound.settimeout.side_effect = OSError(5, canary)
            connection.close.side_effect = outbound.close
            with (
                patch("api_prediction.http.client.HTTPSConnection", return_value=connection),
                patch("api_prediction.connect_bound", return_value=outbound),
                self.assertRaisesRegex(RuntimeError, "connect: OSError"),
            ):
                client._exchange(client.output / "setup", "GET", "/", "", None, {}, 65536)
            self.assertIs(connection.sock, outbound)
            outbound.close.assert_called_once()
            connection.close.assert_called_once()

    def test_corrupted_xyz_preflight_refuses_before_any_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, calls, _source, _canary = upload_client(Path(temporary), fault=None)
            with (
                patch("api_prediction._fixture_bytes", return_value=b"changed XYZ"),
                self.assertRaisesRegex(AssertionError, "original XYZ"),
            ):
                submit(client, {"specification": "Use attached XYZ."}, case_kind="upload")
            self.assertEqual(calls, [])

    def test_upload_errors_never_retry_or_advance_and_secret_bodies_never_reach_files_or_rendered_failure(self):
        _actual, retained, _receipt, _metadata, _interpreter, _upload, _source, _expected = uploaded_documents()
        for fault in (
            "hostile-url",
            "header-injection",
            "cap-error-body",
            "cap-schema",
            "cap-expired",
            "cap-json",
            "put-error-body",
            "lost-put",
            "wrong-finalize",
            "wrong-attach",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                client, calls, _source, canary = upload_client(Path(temporary), fault=fault)
                with (
                    patch("api_prediction._retained_science", return_value=retained),
                    patch("api_prediction.sign_request", side_effect=synthetic_signer),
                    self.assertRaises((RuntimeError, AssertionError)) as caught,
                ):
                    submit(client, {"specification": "Use attached atom_order.xyz, XYZ, angstrom, record 1."}, case_kind="upload")
                byte_requests = [call for call in calls if call[1].startswith("/upload/v1/")]
                self.assertEqual(
                    len(byte_requests),
                    0
                    if fault in ("hostile-url", "header-injection", "cap-error-body", "cap-schema", "cap-expired", "cap-json")
                    else 1,
                )
                self.assertFalse(any(path.endswith("/state") for _method, path, *_rest in calls))
                if fault in ("put-error-body", "lost-put"):
                    self.assertFalse(
                        any(
                            path.endswith("/finalize") or (path.endswith("/uploads") and path.startswith("/v1/jobs/"))
                            for _method, path, *_rest in calls
                        )
                    )
                if fault == "wrong-finalize":
                    self.assertFalse(any(path.startswith("/v1/jobs/") for _method, path, *_rest in calls))
                self.assertFalse(any(canary.encode() in path.read_bytes() for path in client.output.iterdir()))
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn(canary, rendered)
                if fault == "hostile-url":
                    self.assertIn("different store or path", rendered)
                elif fault == "header-injection":
                    self.assertIn("visible ASCII header", rendered)
                elif fault == "cap-error-body":
                    self.assertIn("HTTP 403", rendered)
                elif fault == "put-error-body":
                    self.assertIn("HTTP 500", rendered)
                elif fault == "cap-schema":
                    self.assertIn("authorization fields differ", rendered)
                elif fault == "cap-expired":
                    self.assertIn("canonical expiry", rendered)

    def test_upload_preflight_rejects_incompatible_actual_pinned_coordinates_before_any_dispatch(self):
        _actual, retained, _receipt, _metadata, _interpreter, _upload, _source, _expected = uploaded_documents()
        retained["geometries"][0]["float32_coordinates"][0][0] += 0.25
        with tempfile.TemporaryDirectory() as temporary:
            client, calls, _source, _canary = upload_client(Path(temporary), fault=None)
            with (
                patch("api_prediction._retained_science", return_value=retained),
                self.assertRaisesRegex(AssertionError, "binary32"),
            ):
                submit(client, {"specification": "Use attached XYZ."}, case_kind="upload")
            self.assertEqual(calls, [])

    def test_upload_source_order_positions_request_or_predictions_cannot_drift(self):
        for fault in (
            "source",
            "order",
            "positions",
            "digest",
            "coordinate",
            "prediction",
            "coverage",
            "missing-coverage",
            "turns",
        ):
            with self.subTest(fault=fault):
                actual, retained, receipt, metadata, interpreter, upload, source, expected = uploaded_documents()
                geometry = actual["geometries"][0]
                if fault == "source":
                    geometry["source"]["upload_ref"] = "upload:sha256:" + "0" * 64
                elif fault == "order":
                    geometry["prepared_request"]["atoms"][:2] = ["Cl", "H"]
                elif fault == "positions":
                    geometry["source"]["positions"][0] = 1
                elif fault == "digest":
                    geometry["request_sha256"] = "0" * 64
                elif fault == "coordinate":
                    geometry["prepared_request"]["coordinates"][0][0] = math.nextafter(
                        geometry["prepared_request"]["coordinates"][0][0], math.inf
                    )
                elif fault == "prediction":
                    geometry["predictions"][0]["shift_ppm"] += 0.01
                elif fault == "coverage":
                    geometry["coverage"]["hydrogen_completeness"] = "complete"
                elif fault == "missing-coverage":
                    del geometry["coverage"]
                else:
                    actual["interpretation"]["turns_started"] = 1
                with self.assertRaises(AssertionError):
                    compare_upload_scientific_documents(
                        actual,
                        retained,
                        receipt,
                        metadata,
                        interpreter=interpreter,
                        upload=upload,
                        source=source,
                        expected=expected,
                    )

    def test_generated_observed_stereochemistry_must_be_retained_with_its_digest(self):
        for fault in ("omitted", "mismatched"):
            with self.subTest(fault=fault):
                actual, retained, receipt, metadata, interpreter, expected = generated_documents()
                preparation = actual["geometries"][0]["preparation"]
                if fault == "omitted":
                    del preparation["stereochemistry"]
                else:
                    preparation["stereochemistry_sha256"] = "0" * 64
                with self.assertRaisesRegex(AssertionError, "stereochemistry"):
                    compare_smiles_scientific_documents(
                        actual, retained, receipt, metadata, interpreter=interpreter, expected=expected
                    )

    def test_generated_ccl_wrong_parents_order_chlorine_row_count_or_identity_fail(self):
        for fault in (
            "parents",
            "order",
            "chlorine-row",
            "count",
            "digest",
            "float32",
            "nonfinite",
            "source",
            "coverage",
            "turns",
        ):
            with self.subTest(fault=fault):
                actual, retained, receipt, metadata, interpreter, expected = generated_documents()
                geometry = actual["geometries"][0]
                if fault == "parents":
                    actual["generation"]["provenance"]["added_hydrogen_parents"][0][1] = 1
                    actual["generation"]["provenance_sha256"] = hashlib.sha256(
                        json.dumps(actual["generation"]["provenance"], sort_keys=True, allow_nan=False).encode()
                    ).hexdigest()
                elif fault == "order":
                    geometry["prepared_request"]["atoms"][:2] = ["Cl", "C"]
                elif fault == "chlorine-row":
                    geometry["predictions"][1].update(atom_index=1, element="Cl")
                elif fault == "count":
                    actual["generation"]["selection"]["requested_count"] = 3
                elif fault == "digest":
                    geometry["request_sha256"] = "0" * 64
                elif fault == "float32":
                    geometry["float32_coordinates"][0][0] += 0.25
                elif fault == "nonfinite":
                    geometry["predictions"][0]["shift_ppm"] = math.inf
                elif fault == "source":
                    actual["generation"]["selection"]["source_sha256"] = "0" * 64
                elif fault == "coverage":
                    geometry["coverage"]["predicted_hydrogen_atoms"] = 2
                else:
                    actual["interpretation"]["turns_started"] = 2
                with self.assertRaises(AssertionError):
                    compare_smiles_scientific_documents(
                        actual, retained, receipt, metadata, interpreter=interpreter, expected=expected
                    )

    def test_real_atom_order_binary32_coordinate_prediction_and_interpretation_changes_fail(self):
        for fault in ("order", "binary32", "prediction", "unit", "head", "model", "turns", "endpoint"):
            with self.subTest(fault=fault):
                actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
                geometry = actual["geometries"][0]
                if fault == "order":
                    geometry["prepared_request"]["atoms"][:2] = ["Cl", "H"]
                elif fault == "binary32":
                    geometry["float32_coordinates"][0][0] += 0.25
                elif fault == "prediction":
                    geometry["predictions"][0]["shift_ppm"] += 0.0001
                elif fault == "unit":
                    geometry["preparation"]["source_unit"] = "angstrom"
                elif fault == "head":
                    geometry["loaded_elements"] = ["H"]
                elif fault == "model":
                    actual["selected_models"]["H"] = "changed-checkpoint"
                elif fault == "turns":
                    actual["interpretation"]["turns_started"] = 2
                else:
                    actual["interpretation"]["routes"][0]["endpoint"]["model"] = "different-model"
                with self.assertRaises(AssertionError):
                    compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter)

    def test_changed_binary64_value_cannot_pass_merely_because_binary32_is_unchanged(self):
        actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
        request = actual["geometries"][0]["prepared_request"]
        original = request["coordinates"][0][0]
        changed = math.nextafter(original, math.inf)
        self.assertEqual(struct.pack("f", original), struct.pack("f", changed))
        request["coordinates"][0][0] = changed
        with self.assertRaisesRegex(AssertionError, "prepared_request"):
            compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter)

    def test_pinned_retained_receipt_hash_is_still_checked_before_comparison(self):
        with (
            patch("api_prediction._fixture_bytes", return_value=b"{}"),
            self.assertRaisesRegex(AssertionError, "independent retained"),
        ):
            compare_science(b"{}", {}, {}, interpreter=None, case_kind="pasted")

    def test_scripted_source_positions_remain_exact(self):
        actual, retained, receipt, metadata, _interpreter, _source = scientific_documents()
        actual["geometries"][0]["source"]["positions"][0] = 2
        with self.assertRaises(AssertionError):
            compare_scientific_documents(actual, retained, receipt, metadata, interpreter=None)

    def test_real_source_identity_and_actual_line_positions_are_required(self):
        for fault in (
            "missing-sha256",
            "missing-byte_length",
            "missing-positions",
            "bad-digest",
            "empty-length",
            "wrong-count",
            "repeated-line",
        ):
            with self.subTest(fault=fault):
                actual, retained, receipt, metadata, interpreter, _text = scientific_documents()
                source = actual["geometries"][0]["source"]
                if fault.startswith("missing-"):
                    del source[fault.removeprefix("missing-")]
                elif fault == "bad-digest":
                    source["sha256"] = "unverified"
                elif fault == "empty-length":
                    source["byte_length"] = 0
                elif fault == "wrong-count":
                    source["positions"].pop()
                else:
                    source["positions"][1] = source["positions"][0]
                with self.assertRaises(AssertionError):
                    compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter)

    def test_readback_expiry_retains_the_created_job_without_resubmission(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, clock, calls = clocked_client(Path(temporary), ready_at=1000)
            receipt = submit(client, {"specification": "Fixed supplied coordinates in nm"}, case_kind="pasted")
            created = (client.output / "created.json").read_bytes()
            with (
                patch("api_prediction.time.monotonic", clock.monotonic),
                patch("api_prediction.time.sleep", clock.sleep),
                self.assertRaisesRegex(TimeoutError, "no mutation was retried"),
            ):
                receive(client, receipt, readback_seconds=900, case_kind="pasted")
            self.assertEqual(clock.seconds, 900)
            self.assertEqual((client.output / "created.json").read_bytes(), created)
            self.assertFalse((client.output / "scientific-result.json").exists())
            self.assertEqual([method for _label, method, *_rest in calls if method != "GET"], ["POST", "PUT", "PUT"])

    def test_main_arms_the_selected_process_budget_before_credentials_or_dispatch(self):
        handlers = {}

        def remember(signum, handler):
            handlers[signum] = handler

        def expire(_seconds):
            import signal  # noqa: PLC0415 -- This fixture invokes the exact handler main registered.

            handlers[signal.SIGALRM](signal.SIGALRM, None)

        arguments = [
            "api_prediction",
            "--address",
            "192.0.2.1",
            "--interface",
            "fixture0",
            "--project-ref",
            "project:user-demo",
            "--user-id",
            "demo",
            "--readback-seconds",
            "900",
        ]
        for case_kind, process_seconds in (("pasted", 960), ("upload", 1020)):
            with (
                self.subTest(case_kind=case_kind),
                patch("sys.argv", [*arguments, "--process-seconds", str(process_seconds), "--case-kind", case_kind]),
                patch("api_prediction.signal.signal", side_effect=remember),
                patch("api_prediction.signal.alarm", side_effect=expire) as alarm,
                patch("api_prediction.submit") as dispatch,
                patch("api_prediction.Path.read_bytes") as read,
                self.assertRaisesRegex(TimeoutError, f"{process_seconds} seconds"),
            ):
                main()
            alarm.assert_called_once_with(process_seconds)
            dispatch.assert_not_called()
            read.assert_not_called()

    def test_late_received_result_is_retained_but_cannot_pass_after_readback_expiry(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, clock, calls = clocked_client(Path(temporary), ready_at=0)
            receipt = submit(client, {"specification": "Fixed supplied coordinates in nm"}, case_kind="pasted")
            created = (client.output / "created.json").read_bytes()
            request = client.request

            def late_result(*args, **kwargs):
                response = request(*args, **kwargs)
                if args[0] == "result":
                    clock.seconds = 901
                return response

            client.request = late_result
            with (
                patch("api_prediction.time.monotonic", clock.monotonic),
                patch("api_prediction.time.sleep", clock.sleep),
                patch("api_prediction.compare_science"),
                self.assertRaisesRegex(TimeoutError, "received result exceeded"),
            ):
                receive(client, receipt, readback_seconds=900, case_kind="pasted")
            self.assertEqual((client.output / "created.json").read_bytes(), created)
            self.assertTrue((client.output / "scientific-result.json").exists())
            self.assertEqual([method for _label, method, *_rest in calls if method != "GET"], ["POST", "PUT", "PUT"])
