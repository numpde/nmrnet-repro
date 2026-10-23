"""Synthetic documents for causal qualification comparison tests; no science runs."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import struct
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from api_prediction import Client, canonicalize_job_create_request_body
from nmr_api.public.upload_create_request import parse_upload_create_request
from nmr_api.security.user_ids import UserAuthorityRefs


def scientific_documents():
    receipt = {"job_ref": "job:new", "specification_fingerprint": "sha256:" + "a" * 64}
    metadata = {"execution_attempt_ref": "execution_attempt:new"}
    interpreter = {"configuration_id": "selected-fixture", "model": "selected-model", "configuration_sha256": "b" * 64}
    source_text = "H 0.1027 0 -0.0363\nCl 0 0 0.178\nC 0 0 0\nH -0.0514 0.089 -0.0363\nH -0.0514 -0.089 -0.0363\n"
    request = {
        "atoms": ["H", "Cl", "C", "H", "H"],
        "coordinate_unit": "angstrom",
        "coordinates": [[1.027, 0, -0.363], [0, 0, 1.78], [0, 0, 0], [-0.514, 0.89, -0.363], [-0.514, -0.89, -0.363]],
    }
    geometry = {
        "source": {
            "job_ref": receipt["job_ref"],
            "input_fingerprint": receipt["specification_fingerprint"],
            "origin": "interpreter_supplied_coordinates",
            "format": "table",
            "positions_relative_to": "received_source_text",
            "record_number": 1,
            "position_kind": "line_number",
            "positions": [1, 2, 3, 4, 5],
            "sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            "byte_length": len(source_text.encode()),
        },
        "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        "prepared_request": request,
        "float32_coordinates": [
            [struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in request["coordinates"]
        ],
        "preparation": {
            "method": "coordinate_table",
            "source_unit": "nm",
            "scale_to_angstrom": 10,
            "conversion_reference": "fixed-nm-conversion",
        },
        "prediction_unit": "ppm",
        "loaded_elements": ["C", "H"],
        "predictions": [
            {"atom_index": index, "element": atom, "shift_ppm": index + 0.125}
            for index, atom in enumerate(request["atoms"])
            if atom in ("H", "C")
        ],
    }
    retained = {
        "job_ref": "job:retained",
        "execution_attempt_ref": "execution_attempt:retained",
        "input_fingerprint": "sha256:retained",
        "selected_models": {"H": "fixed-checkpoint-h", "C": "fixed-checkpoint-c"},
        "runtime": {"fixed": "runtime"},
        "execution": {"image_id": "sha256:" + "c" * 64},
        "geometries": [geometry],
    }
    actual = copy.deepcopy(retained)
    actual.update(
        job_ref=receipt["job_ref"],
        execution_attempt_ref=metadata["execution_attempt_ref"],
        input_fingerprint=receipt["specification_fingerprint"],
        output={"shift_significant_digits": 17},
    )
    actual["interpretation"] = {
        "schema_id": "nmrnet.input_interpretation.v1",
        "outcome": "selected",
        "operations": ["prepare_pasted"],
        "turns_started": 1,
        "corrections": 0,
        "routes": [{"endpoint": dict(interpreter), "turns_started": 1}],
    }
    return actual, retained, receipt, metadata, interpreter, source_text


def generated_documents():
    """Synthetic CCl/N1 observations, with no claimed numerical reference."""
    return _generated_documents("implicit_hydrogens")


def generated_multiple_documents():
    """Three synthetic CCCC observations may share values but have actual IDs."""
    return _generated_documents("flexible_multiple")


def _generated_documents(case_name):
    actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
    expected = next(
        case for case in json.loads(Path("/fixture/smiles.json").read_bytes())["normative"] if case["name"] == case_name
    )
    count = expected["requested_conformers"]
    hydrogen_count = len(expected["expected_added_hydrogen_parents"])
    source_sha256 = hashlib.sha256(expected["smiles"].encode()).hexdigest()
    provenance = {
        "source_sha256": source_sha256,
        "requested_count": count,
        "source_atoms": [
            {"source_atom_index": index, "element": element} for index, element in enumerate(expected["source_atoms"])
        ],
        "added_hydrogen_parents": copy.deepcopy(expected["expected_added_hydrogen_parents"]),
        "runtime": {"python": "fixture-python", "rdkit": "fixture-rdkit"},
        "embedding": {"preset": "ETKDGv3", "requested_settings": {"randomSeed": 42}},
    }
    actual["generation"] = {
        "provenance": provenance,
        "provenance_sha256": hashlib.sha256(json.dumps(provenance, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        "selection": {
            "job_ref": receipt["job_ref"],
            "input_fingerprint": receipt["specification_fingerprint"],
            "origin": "interpreter_supplied_smiles",
            "byte_length": len(expected["smiles"].encode()),
            "source_sha256": source_sha256,
            "requested_count": count,
        },
    }
    geometry = actual["geometries"][0]
    stereochemistry = {
        "origin": "generated_coordinates",
        "method": "RDKit.AssignStereochemistryFrom3D(replaceExistingTags=True)",
        "atoms": [],
        "bonds": [],
    }
    request = {
        "coordinate_unit": "angstrom",
        "atoms": expected["source_atoms"] + ["H"] * hydrogen_count,
        "coordinates": [[float(index), 0.1, -0.25] for index in range(len(expected["source_atoms"]) + hydrogen_count)],
    }
    geometry.update(
        geometry_id="geometry-1",
        prepared_request=request,
        request_sha256=hashlib.sha256(json.dumps(request, separators=(",", ":")).encode()).hexdigest(),
        float32_coordinates=[[struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in request["coordinates"]],
        source={"kind": "smiles", "sha256": source_sha256, "conformer_id": 0},
        preparation={
            "method": "smiles",
            "coordinate_unit": "angstrom",
            "stereochemistry": stereochemistry,
            "stereochemistry_sha256": hashlib.sha256(json.dumps(stereochemistry, sort_keys=True).encode()).hexdigest(),
        },
        coverage={
            "basis": "smiles_graph_expansion",
            "source_explicit_hydrogen_atoms": 0,
            "added_hydrogen_atoms": hydrogen_count,
            "coordinate_hydrogen_atoms": hydrogen_count,
            "predicted_hydrogen_atoms": hydrogen_count,
            "hydrogen_completeness": "not_independently_validated",
        },
        predictions=[
            {"atom_index": index, "element": atom, "shift_ppm": index + 0.25}
            for index, atom in enumerate(request["atoms"])
            if atom in ("C", "H")
        ],
    )
    if count == 3:
        actual["geometries"] = [copy.deepcopy(geometry) for _ in range(count)]
        for position, (conformer, identifier) in enumerate(zip(actual["geometries"], (2, 7, 11), strict=True), start=1):
            conformer["geometry_id"] = f"geometry-{position}"
            conformer["source"]["conformer_id"] = identifier
    actual["interpretation"]["operations"] = ["prepare_smiles"]
    return actual, retained, receipt, metadata, interpreter, expected


class QualificationClock:
    """Advance only when the qualification requests its normal polling pause."""

    def __init__(self):
        self.seconds = 0

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


def clocked_client(output, *, ready_at):
    """Use the real client/submit/readback with finite in-process response data."""
    actual, _retained, receipt, metadata, interpreter, _text = scientific_documents()
    identity = {
        "origin": "https://api.example:9443",
        "authority": "api.example:9443",
        "topology": "dev-local",
        "provider_ref": "provider:fixture",
        "interpretation_scripted": False,
        "interpreter": interpreter,
    }
    with patch("api_prediction.ssl.create_default_context", return_value=None):
        client = Client(
            output,
            Path("/synthetic/unused-key.json"),
            "192.0.2.1",
            "fixture0",
            identity,
            "project:user-demo",
            UserAuthorityRefs("demo"),
        )
    clock, calls = QualificationClock(), []
    content = json.dumps(actual).encode()
    result = {
        "analysis_result_ref": "analysis_result:fixture",
        "provider_ref": identity["provider_ref"],
        "execution_attempt_ref": metadata["execution_attempt_ref"],
        "result_byte_length": len(content),
        "result_fingerprint": "sha256:" + hashlib.sha256(content).hexdigest(),
    }

    def request(label, method, path, query="", body=None, status=200):
        calls.append((label, method, path, query, body, status))
        if label == "directory":
            return {"providers": [{"provider_ref": identity["provider_ref"]}]}
        if label == "create":
            command = canonicalize_job_create_request_body(body)
            created = {
                name: getattr(command, name)
                for name in (
                    "operation_ref",
                    "project_ref",
                    "analysis_kind_ref",
                    "request_fingerprint",
                    "specification_fingerprint",
                )
            }
            return created | {"job_ref": receipt["job_ref"], "submitter_ref": client.user.principal_ref, "replayed": False}
        if label == "select":
            return {"provider_refs": [identity["provider_ref"]]}
        if label == "open":
            return {"job": {"job_ref": receipt["job_ref"], "state": "open"}}
        if label in ("attempts", "final-attempts"):
            attempts = (
                [{"execution_attempt_ref": metadata["execution_attempt_ref"], "state": "succeeded"}]
                if clock.seconds >= ready_at
                else []
            )
            return {"job_ref": receipt["job_ref"], "next_cursor": None, "attempts": attempts}
        if label == "results":
            return {"job_ref": receipt["job_ref"], "next_cursor": None, "results": [result] if clock.seconds >= ready_at else []}
        if label == "result":
            import base64  # noqa: PLC0415 -- Only the readback response fixture needs its wire encoding.

            return result | {"job_ref": receipt["job_ref"], "canonical_result_base64": base64.b64encode(content).decode()}
        raise AssertionError("The clock fixture received an unexpected API operation")

    client.request = request
    return client, clock, calls


class UploadResponse:
    """Only the already-received response metadata used by this finite client."""

    def __init__(self, status, *, topology):
        self.status = status
        self.headers = [("Nmr-Api-Topology", topology), ("X-Request-Id", "synthetic-request-id")]

    def getheaders(self):
        return self.headers

    def getheader(self, name):
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)


def synthetic_signer(_arguments):
    sys.stdout.write("Signature: synthetic-api-signature\n")


def upload_client(output, *, fault, source=None, existing=None):
    """Run real signing/operation owners around finite in-process HTTP replies."""
    client, _clock, _calls = clocked_client(output, ready_at=0)
    del client.request
    calls = []
    upload_ref = "upload:sha256:" + "d" * 64
    if source is None:
        source = Path("/fixture/atom_order.xyz").read_bytes()
    canary = "synthetic-publication-secret-canary"
    reservation = dict(existing or {})

    def exchange(prefix, method, path, _query, body, headers, maximum_bytes):
        assert maximum_bytes == (65536 if path.endswith("/publish-capability") or path.startswith("/upload/v1/") else 1048576)
        calls.append((method, path, body, headers))
        status, document = 200, {}
        if path == "/v1/providers":
            document = {"providers": [{"provider_ref": client.identity["provider_ref"]}]}
        elif method == "GET" and path == "/v1/uploads/" + upload_ref:
            document = reservation | {"content_hash": None, "finalized_at": None}
            if fault == "already-finalized":
                document["content_hash"] = "sha256:" + hashlib.sha256(source).hexdigest()
        elif path == "/v1/uploads":
            command = parse_upload_create_request(body)
            reservation.update(
                schema_id="nmr.upload.create.response.v1",
                upload_ref=upload_ref,
                project_ref=client.project_ref,
                byte_length=len(source),
                created_at="2026-10-21T00:00:00Z",
                creator_ref=client.user.principal_ref,
                operation_ref=command.operation_ref,
                request_fingerprint=command.request_fingerprint,
                replayed=False,
            )
            status, document = 201, reservation
        elif path.endswith("/publish-capability"):
            document = {
                "schema_id": "nmr.upload.publish_capability.response.v1",
                "upload_ref": upload_ref,
                "method": "PUT",
                "expected_byte_length": len(source),
                "capability": canary,
                "upload_url": client.identity["origin"] + "/upload/v1/uploads/" + upload_ref + "/bytes",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            }
            if fault == "hostile-url":
                document["upload_url"] = "https://other.example/upload/v1/uploads/" + upload_ref + "/bytes"
            elif fault == "header-injection":
                document["capability"] += "\r\n"
            elif fault == "cap-error-body":
                status, document = 403, {"detail": canary}
            elif fault == "cap-schema":
                document["unexpected"] = canary
            elif fault == "cap-expired":
                document["expires_at"] = "2000-01-01T00:00:00Z"
            elif fault == "cap-json":
                response = UploadResponse(200, topology=client.identity["topology"])
                client._capture_head(prefix, response)
                return response, ('{"capability":"' + canary).encode()
        elif path.startswith("/upload/v1/"):
            if fault == "lost-put":
                raise OSError("synthetic transfer interrupted " + canary)
            status, document = (500, {"detail": canary}) if fault == "put-error-body" else (201, {"ignored": canary})
        elif path.endswith("/finalize"):
            document = {key: reservation[key] for key in ("upload_ref", "project_ref", "byte_length", "created_at")}
            document.update(content_hash="sha256:" + hashlib.sha256(source).hexdigest(), replayed=False)
            if fault == "wrong-finalize":
                document["content_hash"] = "sha256:" + "0" * 64
        elif path == "/v1/jobs":
            command = canonicalize_job_create_request_body(body)
            status = 201
            document = {
                name: getattr(command, name)
                for name in (
                    "operation_ref",
                    "project_ref",
                    "analysis_kind_ref",
                    "request_fingerprint",
                    "specification_fingerprint",
                )
            }
            document.update(job_ref="job:upload-new", submitter_ref=client.user.principal_ref, replayed=False)
        elif path.endswith("/uploads"):
            document = {"upload_refs": [] if fault == "wrong-attach" else [upload_ref]}
        elif path.endswith("/providers"):
            document = {"provider_refs": [client.identity["provider_ref"]]}
        elif path.endswith("/state"):
            document = {"job": {"job_ref": "job:upload-new", "state": "open"}}
        else:
            raise AssertionError("Unexpected synthetic Upload operation")
        response = UploadResponse(status, topology=client.identity["topology"])
        client._capture_head(prefix, response)
        return response, json.dumps(document).encode()

    client._exchange = exchange
    return client, calls, source, canary


def uploaded_sdf_documents():
    """Two synthetic carbon-only records exercise exact comparison, not accuracy."""
    actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
    geometry = retained["geometries"][0]
    geometry["geometry_id"] = "geometry-1"
    geometry["coverage"] = {
        "basis": "supplied_coordinates",
        "supplied_hydrogen_atoms": 0,
        "predicted_hydrogen_atoms": 0,
        "hydrogen_completeness": "not_established_from_coordinates",
    }
    geometry["source"] = {
        "kind": "upload",
        "upload_ref": "upload:sha256:" + "a" * 64,
        "sha256": "c8007f5a1a80ad080bb972caf005bbb4fedcd5a9bd15b892e1b640d2f01e4374",
        "byte_length": 1010,
        "record_number": 1,
        "positions": [1, 2, 3, 4],
    }
    geometry["prepared_request"] = {
        "coordinate_unit": "angstrom",
        "atoms": ["C"] * 4,
        "coordinates": [[i, 0, 0] for i in range(4)],
    }
    geometry["float32_coordinates"] = copy.deepcopy(geometry["prepared_request"]["coordinates"])
    geometry["loaded_elements"] = ["C"]
    geometry["predictions"] = [{"atom_index": i, "element": "C", "shift_ppm": i + 0.25} for i in range(4)]
    retained["geometries"] = [copy.deepcopy(geometry), copy.deepcopy(geometry)]
    retained["geometries"][1]["geometry_id"] = "geometry-2"
    retained["geometries"][1]["source"]["record_number"] = 2
    upload = {"upload_ref": "upload:sha256:" + "d" * 64, "content_hash": "sha256:" + "e" * 64, "byte_length": 1152}
    actual["geometries"] = copy.deepcopy(retained["geometries"])
    for item in actual["geometries"]:
        item["source"].update(
            upload_ref=upload["upload_ref"],
            archive={"sha256": "e" * 64, "byte_length": 1152, "member_name": "butane_conformers.sdf"},
        )
    actual["schema_id"] = "nmrnet.coordinate_shifts.result.v1"
    actual["interpretation"].update(
        operations=["inspect_uploads", "inspect_upload", "prepare_upload"],
        turns_started=3,
        turn_limit=6,
        routes=[{"endpoint": interpreter, "turns_started": 3}],
    )
    return actual, retained, receipt, metadata, interpreter, upload


def uploaded_documents():
    """Actual authored XYZ values plus synthetic qualified model observations."""
    actual, retained, receipt, metadata, interpreter, _text = scientific_documents()
    expected = json.loads(Path("/fixture/expected.json").read_bytes())["geometries"]["chloromethane"]
    source = Path("/fixture/atom_order.xyz").read_bytes()
    request = {
        "coordinate_unit": "angstrom",
        "atoms": copy.deepcopy(expected["atoms"]),
        "coordinates": copy.deepcopy(expected["coordinates_angstrom"]),
    }
    coordinates = [[struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in request["coordinates"]]
    retained["geometries"][0]["float32_coordinates"] = copy.deepcopy(coordinates)
    coverage = {
        "basis": "supplied_coordinates",
        "supplied_hydrogen_atoms": 3,
        "predicted_hydrogen_atoms": 3,
        "hydrogen_completeness": "not_established_from_coordinates",
    }
    retained["geometries"][0]["coverage"] = copy.deepcopy(coverage)
    actual["geometries"][0]["coverage"] = coverage
    actual["geometries"][0].update(
        prepared_request=request,
        request_sha256=hashlib.sha256(json.dumps(request, separators=(",", ":")).encode()).hexdigest(),
        float32_coordinates=coordinates,
        source={
            "sha256": hashlib.sha256(source).hexdigest(),
            "record_number": 1,
            "position_kind": "line_number",
            "positions": [3, 4, 5, 6, 7],
            "upload_ref": "upload:sha256:" + "d" * 64,
            "byte_length": len(source),
        },
        preparation={
            "method": "xyz",
            "source_unit": "angstrom",
            "scale_to_angstrom": 1.0,
            "conversion_reference": "identity (angstrom)",
        },
    )
    actual["interpretation"].update(
        operations=["inspect_uploads", "prepare_upload"], turns_started=2, routes=[{"endpoint": interpreter, "turns_started": 2}]
    )
    upload = {
        "upload_ref": "upload:sha256:" + "d" * 64,
        "byte_length": len(source),
        "content_hash": "sha256:" + hashlib.sha256(source).hexdigest(),
    }
    return actual, retained, receipt, metadata, interpreter, upload, source, expected


def clocked_upload_client(output, *, ready_at):
    """Compose real publication/submit/readback with one finite synthetic clock."""
    client, calls, _source, _canary = upload_client(output, fault=None)
    clock = QualificationClock()
    exchange = client._exchange
    actual, _retained, _receipt, metadata, _interpreter, _upload, _source, _expected = uploaded_documents()

    def response(prefix, method, path, query, body, headers, maximum_bytes):
        if method != "GET" or not path.startswith("/v1/jobs/"):
            received = exchange(prefix, method, path, query, body, headers, maximum_bytes)
            clock.sleep(10)
            return received
        calls.append((method, path, body, headers))
        receipt = json.loads((client.output / "created.json").read_bytes())
        actual.update(job_ref=receipt["job_ref"], input_fingerprint=receipt["specification_fingerprint"])
        content = json.dumps(actual).encode()
        result = {
            "analysis_result_ref": "analysis_result:clock-fixture",
            "provider_ref": client.identity["provider_ref"],
            "execution_attempt_ref": metadata["execution_attempt_ref"],
            "result_byte_length": len(content),
            "result_fingerprint": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
        label = prefix.name.split("-", 1)[1]
        ready = clock.seconds >= ready_at
        if label in ("attempts", "final-attempts"):
            document = {
                "job_ref": receipt["job_ref"],
                "next_cursor": None,
                "attempts": [{"execution_attempt_ref": metadata["execution_attempt_ref"], "state": "succeeded"}] if ready else [],
            }
        elif label == "results":
            document = {"job_ref": receipt["job_ref"], "next_cursor": None, "results": [result] if ready else []}
        elif label == "result":
            import base64  # noqa: PLC0415 -- This finite response encodes already-owned synthetic bytes.

            document = result | {"job_ref": receipt["job_ref"], "canonical_result_base64": base64.b64encode(content).decode()}
        else:
            raise AssertionError("Unexpected Upload readback operation")
        head = UploadResponse(200, topology=client.identity["topology"])
        client._capture_head(prefix, head)
        return head, json.dumps(document).encode()

    client._exchange = response
    return client, clock, calls
