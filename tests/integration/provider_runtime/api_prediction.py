"""One actual API Job through the API-owned client contract.

Uses the installed nmr-api-v1 user-artifact validator, signing recipe, Job-create
canonicalizer and result decoders (e7790cf6). No mutation is retried, redirected
or reconstructed after dispatch. Raw bodies and receipts remain in private output;
keys/signatures/transcripts do not. The caller selects pasted input, CCl/N1 or
CCCC/N3 generation, an attached XYZ fixture or a two-record SDF ZIP. Supplied coordinates retain their
independent numerical oracles; generation checks observed provenance and H/C
coverage without reference shift values. These cases do not qualify GUI behavior
or experimental accuracy.
"""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
import re
import signal
import ssl
import struct
import tempfile
import time
import uuid
import zipfile
from contextlib import redirect_stdout, suppress
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

from nmr_api.canonical_base64 import decode_canonical_base64
from nmr_api.canonical_json import decode_strict_json
from nmr_api.discovery_examples.sign_request import main as sign_request
from nmr_api.json_datetime import CANONICAL_JSON_DATETIME_PATTERN
from nmr_api.public.job_create_request import canonicalize_job_create_request_body
from nmr_api.public.job_upload_set_replace_request import parse_job_upload_set_replace_request
from nmr_api.public.schema_ids import UPLOAD_PUBLISH_CAPABILITY_RESPONSE_SCHEMA_ID
from nmr_api.public.upload_create_request import parse_upload_create_request
from nmr_api.request_ids import require_request_id
from nmr_api.operations.job_refs import require_job_ref
from nmr_api.security.project_refs import require_project_ref
from nmr_api.security.user_credential_artifacts import validate_user_credential_artifacts
from nmr_api.security.user_ids import UserAuthorityRefs
from nmr_api.upload_refs import require_upload_ref
from nmr_api.upload_store_target import UploadStoreTarget
from nmr_api.upload_transfer_capabilities import UPLOAD_TRANSFER_CAPABILITY_MAX_CHARACTERS
from nmr_api.upload_transfer_http_contract import UPLOAD_BYTES_PUBLISH_METHOD

from tools.bound_http_proxy import connect_bound


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class CarrierFailed(RuntimeError):
    """Safe operation/close observations; arbitrary peer text stays private."""


class Client:
    """Freshly sign each finite request; preserve received bytes before admission."""

    def __init__(self, output, credential, address, interface, identity, project_ref, user):
        self.output, self.credential, self.count = output, credential, 0
        self.address, self.interface = address, interface
        self.identity, self.project_ref, self.user = identity, project_ref, user
        origin = urlsplit(identity["origin"])
        self.host, self.port = origin.hostname, origin.port if origin.port is not None else 443
        self.trust = ssl.create_default_context(cafile="/input/api-ca.crt")

    def _signed_response(self, label, method, path, query, body, maximum_bytes):
        self.count += 1
        prefix = self.output / f"{self.count:03}-{label}"
        Path(str(prefix) + ".request-head.json").write_text(
            json.dumps({"method": method, "authority": self.identity["authority"], "path": path, "query": query}) + "\n"
        )
        signing = [str(self.credential), method, self.identity["authority"], path, query]
        if body is not None:
            with Path(str(prefix) + ".request.json").open("xb") as stream:
                stream.write(body)
            signing.append(str(prefix) + ".request.json")
        headers = io.StringIO()
        with redirect_stdout(headers):
            sign_request(signing)
        admitted_headers = dict(line.split(": ", 1) for line in headers.getvalue().splitlines())
        response, content = self._exchange(prefix, method, path, query, body, admitted_headers, maximum_bytes)
        return prefix, response, content

    def _capture_head(self, prefix, response):
        headers = response.getheaders()
        ids = [value for key, value in headers if key.lower() == "x-request-id"]
        request_id = None
        if len(ids) == 1:
            with suppress(TypeError, ValueError):
                request_id = require_request_id("response request ID", ids[0])
        topology = [value for key, value in headers if key.lower() == "nmr-api-topology"]
        Path(str(prefix) + ".head.json").write_text(
            json.dumps(
                {
                    "status": response.status,
                    "topology": self.identity["topology"] if topology == [self.identity["topology"]] else None,
                    "request_id": request_id,
                }
            )
            + "\n"
        )

    def _exchange(self, prefix, method, path, query, body, headers, maximum_bytes):
        """Use one bound carrier; callers separately own signed JSON or bearer bytes."""
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=10, context=self.trust)
        failures, phase = [], "connect"
        try:
            # Use the same existing bound-address owner as the provider's mapped
            # proxy. TLS and HTTP still use the original canonical authority.
            outbound = connect_bound(self.address, self.port, self.interface)
            connection.sock = outbound
            outbound.settimeout(10)
            connection.sock = self.trust.wrap_socket(outbound, server_hostname=self.host)
            phase = "request dispatch"
            connection.request(method, path + ("?" + query if query else ""), body=body, headers=headers)
            phase = "response headers"
            response = connection.getresponse()
            self._capture_head(prefix, response)
            phase = "response body"
            content = response.read(maximum_bytes + 1)
        except BaseException as cause:
            # Close must also run on interruption. Neither a peer's text
            # nor a secondary close failure may replace the primary observation.
            failures.append(f"{phase}: {type(cause).__name__}, errno={getattr(cause, 'errno', None)}")
        try:
            connection.close()
        except BaseException as cause:
            failures.append(f"connection close: {type(cause).__name__}, errno={getattr(cause, 'errno', None)}")
        if failures:
            raise CarrierFailed("Bound HTTPS exchange failed; " + "; ".join(failures)) from None
        return response, content

    def _api_accepted(self, response, status):
        require(
            response.status == status,
            f"The API returned HTTP {response.status}; expected {status}; inspect retained response metadata",
        )
        require(
            [value for key, value in response.getheaders() if key.lower() == "nmr-api-topology"] == [self.identity["topology"]],
            "The API did not identify the selected topology",
        )

    def request(self, label, method, path, query="", body=None, status=200):
        prefix, response, content = self._signed_response(label, method, path, query, body, 1048576)
        Path(str(prefix) + ".response.json").write_bytes(content)
        self._api_accepted(response, status)
        require(len(content) <= 1048576, f"{label}: response exceeds the qualification byte ceiling")
        return decode_strict_json(content.decode("utf-8"))

    def publication_grant(self, upload):
        """Keep authorization bodies transient on success, rejection and interruption."""
        (self.output / "upload-grant-intent.json").write_text(
            json.dumps(
                {
                    "upload_ref": upload["upload_ref"],
                    "expected_byte_length": upload["byte_length"],
                    "issuance": "not yet confirmed",
                }
            )
            + "\n"
        )
        try:
            _prefix, response, content = self._signed_response(
                "upload-grant", "POST", "/v1/uploads/" + upload["upload_ref"] + "/publish-capability", "", None, 65536
            )
            self._api_accepted(response, 200)
            require(len(content) <= 65536, "Upload publication authorization exceeds its byte ceiling")
            grant = decode_strict_json(content.decode("utf-8"))
        except CarrierFailed as cause:
            raise RuntimeError(
                f"Upload authorization is unconfirmed: {cause}; access may have been issued, no byte publication was dispatched"
            ) from None
        except (OSError, http.client.HTTPException, ValueError) as cause:
            raise RuntimeError(
                f"Cannot receive Upload publication authorization ({type(cause).__name__}, errno={getattr(cause, 'errno', None)}); access may have been issued, but no byte publication was dispatched"
            ) from None
        # All following refusals contain only code-owned summaries. They stay
        # distinct from unsafe decoder/carrier errors without echoing the grant.
        # Closed response authority: nmr-api-v1 e7790cf6,
        # public/openapi_source/metadata.v1.json::UploadPublishCapabilityResponseV1.
        require(
            type(grant) is dict
            and set(grant)
            == {"schema_id", "upload_ref", "method", "upload_url", "expected_byte_length", "capability", "expires_at"},
            "Upload publication authorization fields differ",
        )
        require(
            grant["schema_id"] == UPLOAD_PUBLISH_CAPABILITY_RESPONSE_SCHEMA_ID
            and grant["upload_ref"] == upload["upload_ref"]
            and type(grant["expected_byte_length"]) is int
            and grant["expected_byte_length"] == upload["byte_length"]
            and grant["method"] == UPLOAD_BYTES_PUBLISH_METHOD,
            "Upload publication authorization differs from the reservation",
        )
        require(
            grant["upload_url"] == UploadStoreTarget(self.identity["origin"]).bytes_url(upload["upload_ref"]),
            "Upload publication authorization names a different store or path",
        )
        token = grant["capability"]
        # The API owns the opaque token. Only its header representation is
        # admitted here; http.client errors must never expose token bytes.
        require(
            type(token) is str
            and 0 < len(token) <= UPLOAD_TRANSFER_CAPABILITY_MAX_CHARACTERS
            and all(32 < ord(c) < 127 for c in token),
            "Upload publication bearer is not a bounded visible ASCII header value",
        )
        require(
            type(grant["expires_at"]) is str
            and re.fullmatch(CANONICAL_JSON_DATETIME_PATTERN, grant["expires_at"]) is not None
            and datetime.fromisoformat(grant["expires_at"]) > datetime.now(UTC),
            "Upload publication authorization has no current canonical expiry",
        )
        (self.output / "upload-grant-admitted.json").write_text(
            json.dumps({key: grant[key] for key in ("upload_ref", "expected_byte_length", "expires_at", "method")}) + "\n"
        )
        return grant

    def publish_bytes(self, grant, source):
        """Dispatch one unsigned bearer PUT; received bodies never enter evidence."""
        require(
            grant["upload_url"] == UploadStoreTarget(self.identity["origin"]).bytes_url(grant["upload_ref"])
            and len(source) == grant["expected_byte_length"],
            "Upload publication target or byte length changed before dispatch",
        )
        self.count += 1
        prefix = self.output / f"{self.count:03}-upload-bytes"
        path = urlsplit(grant["upload_url"]).path
        Path(str(prefix) + ".request-head.json").write_text(
            json.dumps(
                {
                    "method": "PUT",
                    "authority": self.identity["authority"],
                    "path": path,
                    "byte_length": len(source),
                    "content_hash": "sha256:" + hashlib.sha256(source).hexdigest(),
                }
            )
            + "\n"
        )
        try:
            response, content = self._exchange(
                prefix,
                "PUT",
                path,
                "",
                source,
                {
                    "Authorization": "Bearer " + grant["capability"],
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(source)),
                },
                65536,
            )
        except CarrierFailed as cause:
            raise RuntimeError(
                f"Upload byte publication is unconfirmed: {cause}; the file may have arrived, no downstream mutation or retry was performed"
            ) from None
        except (OSError, http.client.HTTPException, ValueError) as cause:
            raise RuntimeError(
                f"Upload byte publication is unconfirmed ({type(cause).__name__}, errno={getattr(cause, 'errno', None)}); the file may have arrived, no downstream mutation or retry was performed"
            ) from None
        require(
            response.status == 201 and len(content) <= 65536,
            f"The store returned HTTP {response.status} or exceeded its response ceiling; expected 201; the file may have arrived, no downstream mutation or retry was performed",
        )


def submit(client, case, *, case_kind, upload_receipt=None):
    source = preflight_upload() if case_kind == "upload" else None
    if case_kind == "sdf-zip":
        source = preflight_sdf_zip()
    directory = client.request(
        "directory", "GET", "/v1/providers", f"project_ref={client.project_ref}&analysis_kind_ref=peaks_from_mol&limit=100"
    )
    require(
        any(item["provider_ref"] == client.identity["provider_ref"] for item in directory["providers"]),
        "The dedicated NMRNet offering was not discoverable",
    )
    upload = publish_upload(client, source, case_kind=case_kind, reservation=upload_receipt) if source is not None else None
    body = json.dumps(
        {
            "operation_ref": "nmrnet-first-api:" + uuid.uuid4().hex,
            "project_ref": client.project_ref,
            "analysis_kind_ref": "peaks_from_mol",
            "description": "NMRNet "
            + ("butane" if case_kind in ("smiles-multiple", "sdf-zip") else "chloromethane")
            + "; "
            + ("scripted" if client.identity["interpretation_scripted"] else "real")
            + " interpreter qualification",
            "specification": case["specification"],
            "state": "closed",
        },
        ensure_ascii=False,
    ).encode()
    expected = canonicalize_job_create_request_body(body)
    receipt = client.request("create", "POST", "/v1/jobs", body=body, status=201)
    require(receipt["replayed"] is False, "The first Job-create response unexpectedly reported replay")
    for field in ("operation_ref", "project_ref", "analysis_kind_ref", "request_fingerprint", "specification_fingerprint"):
        require(receipt[field] == getattr(expected, field), f"Job-create receipt differs in {field}")
    require(receipt["submitter_ref"] == client.user.principal_ref, "Job-create receipt names a different submitter")
    (client.output / "created.json").write_text(json.dumps(receipt) + "\n")
    job = "/v1/jobs/" + receipt["job_ref"]
    if upload is not None:
        body = json.dumps({"upload_refs": [upload["upload_ref"]]}).encode()
        expected_attachment = parse_job_upload_set_replace_request(path_params={"job_ref": receipt["job_ref"]}, body=body)
        attached = client.request("attach", "PUT", job + "/uploads", body=body)
        require(
            attached["upload_refs"] == list(expected_attachment.upload_refs),
            "The closed Job Upload set differs; Job was not opened",
        )
    selected = client.request(
        "select", "PUT", job + "/providers", body=json.dumps({"provider_refs": [client.identity["provider_ref"]]}).encode()
    )
    require(selected["provider_refs"] == [client.identity["provider_ref"]], "The selected provider set differs")
    opened = client.request("open", "PUT", job + "/state", body=b'{"state":"open"}')
    require(
        opened["job"]["job_ref"] == receipt["job_ref"] and opened["job"]["state"] == "open",
        "The API did not confirm the intended Job was opened",
    )
    return receipt


def publish_upload(client, source, *, case_kind, reservation=None):
    """Publish once, or explicitly resume an owner-reconciled absent reservation.

    The original Upload must have been inspected by its storage owner first.
    Reissuing its capability follows public/routes.py's same-Upload contract;
    this is an explicit recovery request, never an automatic transfer retry.
    """
    if reservation is not None:
        require(case_kind == "sdf-zip", "Only the reviewed SDF ZIP case can resume an existing Upload")
        upload = decode_strict_json(_fixture_bytes(reservation).decode("utf-8"))
        require_upload_ref("upload_ref", upload["upload_ref"])
        current = client.request("original-upload", "GET", "/v1/uploads/" + upload["upload_ref"])
        require(
            upload["schema_id"] == "nmr.upload.create.response.v1"
            and upload["creator_ref"] == client.user.principal_ref
            and upload["project_ref"] == client.project_ref
            and upload["byte_length"] == len(source)
            and all(
                current[key] == upload[key] for key in ("upload_ref", "creator_ref", "project_ref", "byte_length", "created_at")
            )
            and current["content_hash"] is None
            and current["finalized_at"] is None,
            "Original Upload identity, size or finalization changed; no capability or transfer was issued",
        )
        (client.output / "upload-created.json").write_text(json.dumps(upload) + "\n")
        return publish_reserved_upload(client, upload, source)
    sdf = case_kind == "sdf-zip"
    body = json.dumps(
        {
            "schema_id": "nmr.upload.create.request.v2",
            "operation_ref": "nmrnet-upload:" + uuid.uuid4().hex,
            "project_ref": client.project_ref,
            "byte_length": len(source),
            "filename": "butane_conformers.sdf.zip" if sdf else "atom_order.xyz",
            "media_type": "application/zip" if sdf else "chemical/x-xyz",
            "description": "butane_conformers.sdf; SDF, angstrom, both carbon-only records"
            if sdf
            else "atom_order.xyz; XYZ, angstrom, one record; supplied chloromethane",
        }
    ).encode()
    expected = parse_upload_create_request(body)
    upload = client.request("upload-create", "POST", "/v1/uploads", body=body, status=201)
    require(
        upload["replayed"] is False
        and upload["creator_ref"] == client.user.principal_ref
        and all(
            upload[key] == getattr(expected, key)
            for key in ("operation_ref", "project_ref", "byte_length", "request_fingerprint")
        ),
        "Upload creation receipt differs from the reserved source",
    )
    require_upload_ref("upload_ref", upload["upload_ref"])
    (client.output / "upload-created.json").write_text(json.dumps(upload) + "\n")
    return publish_reserved_upload(client, upload, source)


def publish_reserved_upload(client, upload, source):
    """Use the existing grant/PUT/finalization owners for this exact reservation."""
    grant = client.publication_grant(upload)
    client.publish_bytes(grant, source)
    finalized = client.request("upload-finalize", "POST", "/v1/uploads/" + upload["upload_ref"] + "/finalize")
    require(
        all(finalized[key] == upload[key] for key in ("upload_ref", "project_ref", "byte_length", "created_at"))
        and finalized["content_hash"] == "sha256:" + hashlib.sha256(source).hexdigest()
        and finalized["replayed"] is False,
        "Finalized Upload does not match the original source bytes; no Job was attached or opened",
    )
    (client.output / "upload.json").write_text(json.dumps(finalized) + "\n")
    return finalized


def receive(client, receipt, *, readback_seconds, case_kind, prediction_image_id):
    job = "/v1/jobs/" + receipt["job_ref"]
    deadline = time.monotonic() + readback_seconds
    while time.monotonic() < deadline:
        attempts = client.request("attempts", "GET", job + "/execution-attempts", "limit=100")
        require(
            attempts["job_ref"] == receipt["job_ref"] and attempts["next_cursor"] is None,
            "Attempt page does not describe this single-Job qualification",
        )
        require(
            not any(item["state"] in ("failed", "cancelled", "expired") for item in attempts["attempts"]),
            "The API records an unsuccessful Attempt; inspect its retained provider failure",
        )
        results = client.request("results", "GET", job + "/analysis-results", "limit=100")
        require(
            results["job_ref"] == receipt["job_ref"] and results["next_cursor"] is None,
            "Result page differs or requires another page",
        )
        if results["results"]:
            # Completion can occur between the preceding independent GETs.
            # Re-read history after the accepted result, never resend a mutation.
            attempts = client.request("final-attempts", "GET", job + "/execution-attempts", "limit=100")
            require(attempts["job_ref"] == receipt["job_ref"] and attempts["next_cursor"] is None, "Final Attempt page differs")
            require(
                len(results["results"]) == 1 and len(attempts["attempts"]) == 1,
                "Expected exactly one Attempt and accepted result",
            )
            require(attempts["attempts"][0]["state"] == "succeeded", "The accepted result has no matching succeeded Attempt")
            metadata = results["results"][0]
            require(metadata["provider_ref"] == client.identity["provider_ref"], "A different provider supplied the result")
            require(
                metadata["execution_attempt_ref"] == attempts["attempts"][0]["execution_attempt_ref"],
                "Result and Attempt identities differ",
            )
            document = client.request("result", "GET", job + "/analysis-results/" + metadata["analysis_result_ref"])
            for key in metadata:
                require(document[key] == metadata[key], f"Result metadata/document differs in {key}")
            require(document["job_ref"] == receipt["job_ref"], "Result document names a different Job")
            content = decode_canonical_base64(document["canonical_result_base64"])
            require(len(content) == document["result_byte_length"], "Decoded result byte length differs")
            require(
                "sha256:" + hashlib.sha256(content).hexdigest() == document["result_fingerprint"],
                "Decoded result fingerprint differs",
            )
            (client.output / "scientific-result.json").write_bytes(content)
            compare_science(
                content,
                receipt,
                metadata,
                interpreter=None if client.identity["interpretation_scripted"] else client.identity["interpreter"],
                prediction_image_id=prediction_image_id,
                case_kind=case_kind,
            )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "The received result exceeded the qualification readback deadline; it was retained and no mutation was retried"
                )
            return
        time.sleep(1)
    raise TimeoutError("The bounded prediction readback did not receive a result; no mutation was retried")


def _fixture_bytes(path):
    with Path(path).open("rb") as stream:
        content = stream.read(1048577)
    require(len(content) <= 1048576, "Qualification fixture exceeds its byte ceiling")
    return content


def _retained_science():
    """Use the pinned independent receipt; this run never creates its own oracle."""
    retained_bytes = _fixture_bytes("/input/retained-result.json")
    require(
        hashlib.sha256(retained_bytes).hexdigest() == "9ae5017c42af5c5691554eb3ff6dced7895c6a00ab177975d8aa15ae0c58f59a",
        "The independent retained scientific receipt differs",
    )
    return json.loads(retained_bytes)


def preflight_upload():
    """Bind the actual XYZ bytes and independent numeric oracle before dispatch."""
    source = _fixture_bytes("/fixture/atom_order.xyz")
    # Existing authored fixture at cdf3be8; this pins source bytes, not new shifts.
    require(
        hashlib.sha256(source).hexdigest() == "e6aa2b9c3380c776917615f4db1235cbedf34527c5f92015e66ee49e2c8ed148",
        "The original XYZ Upload fixture bytes differ; no request was dispatched",
    )
    expected = json.loads(_fixture_bytes("/fixture/expected.json"))["geometries"]["chloromethane"]
    retained = _retained_science()["geometries"][0]
    coordinates = [[struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in expected["coordinates_angstrom"]]
    require(
        expected["atoms"] == retained["prepared_request"]["atoms"] and coordinates == retained["float32_coordinates"],
        "Authored XYZ and actual pinned binary32 oracle are incompatible; no request was dispatched",
    )
    return source


def _retained_sdf_science():
    raw = _fixture_bytes("/input/retained-result.json")
    require(
        hashlib.sha256(raw).hexdigest() == "bd5adb6f47ac7b5b739ae4c52ce36d308195bae56b6233c0d07e22918227a3a3",
        "The retained R46 two-record SDF receipt differs; do not create another oracle",
    )
    return json.loads(raw)


def preflight_sdf_zip():
    """Publish the R46 member in a new deterministic ZIP, without GUI effects."""
    source = _fixture_bytes("/fixture/butane_conformers.sdf")
    require(
        len(source) == 1010
        and hashlib.sha256(source).hexdigest() == "c8007f5a1a80ad080bb972caf005bbb4fedcd5a9bd15b892e1b640d2f01e4374",
        "The authored SDF member differs; no request was dispatched",
    )
    retained = _retained_sdf_science()
    require(
        len(retained["geometries"]) == 2
        and all(geometry["prepared_request"]["atoms"] == ["C"] * 4 for geometry in retained["geometries"]),
        "The retained SDF population differs; no request was dispatched",
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(zipfile.ZipInfo("butane_conformers.sdf", date_time=(1980, 1, 1, 0, 0, 0)), source)
    return archive.getvalue()


def compare_science(content, receipt, metadata, *, interpreter, case_kind, prediction_image_id):
    retained = _retained_sdf_science() if case_kind == "sdf-zip" else _retained_science()
    actual = json.loads(content)
    require(
        actual.get("schema_id") == "nmrnet.coordinate_shifts.result.v2",
        "Scientific qualification requires the current v2 result schema",
    )
    output = actual.get("output")
    require(
        type(output) is dict
        and set(output) == {"verbosity", "shift_significant_digits"}
        and output["verbosity"] == "max"
        and type(output["shift_significant_digits"]) is int
        and output["shift_significant_digits"] == 17,
        "Scientific qualification requires the explicitly requested max verbosity and 17 significant digits",
    )
    if case_kind == "pasted":
        compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter, prediction_image_id=prediction_image_id)
    elif case_kind == "sdf-zip":
        compare_sdf_scientific_documents(
            actual,
            retained,
            receipt,
            metadata,
            interpreter=interpreter, prediction_image_id=prediction_image_id,
            upload=json.loads((Path("/output") / "upload.json").read_bytes()),
        )
    elif case_kind in ("smiles", "smiles-multiple"):
        fixture_name = "implicit_hydrogens" if case_kind == "smiles" else "flexible_multiple"
        expected = next(
            case for case in json.loads(Path("/fixture/smiles.json").read_bytes())["normative"] if case["name"] == fixture_name
        )
        compare_smiles_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter, prediction_image_id=prediction_image_id, expected=expected)
    else:
        compare_upload_scientific_documents(
            actual,
            retained,
            receipt,
            metadata,
            interpreter=interpreter, prediction_image_id=prediction_image_id,
            upload=json.loads((Path("/output") / "upload.json").read_bytes()),
            source=_fixture_bytes("/fixture/atom_order.xyz"),
            expected=json.loads(_fixture_bytes("/fixture/expected.json"))["geometries"]["chloromethane"],
        )


def require_selected_interpretation(observation, interpreter, operation):
    """Allow configured correction/fallback; retain actual routes in the result.

    ToolConversation owns the total turn ceiling and ordered endpoint fallback.
    Delivery qualification must not turn a valid correction into another Job.
    """
    turns, corrections = observation["turns_started"], observation["corrections"]
    routes, operations = observation["routes"], observation["operations"]
    require(
        observation["schema_id"] == "nmrnet.input_interpretation.v1"
        and observation["outcome"] == "selected"
        and type(turns) is int and 0 < turns <= interpreter["max_turns"]
        and type(corrections) is int and 0 <= corrections < turns
        and operations and operations[-1] == operation and len(operations) <= turns
        and 0 < len(routes) <= len(interpreter["endpoints"])
        and all(route["endpoint"] == endpoint and type(route["turns_started"]) is int and route["turns_started"] > 0
                for route, endpoint in zip(routes, interpreter["endpoints"]))
        and sum(route["turns_started"] for route in routes) == turns,
        "Selected interpretation differs from the admitted endpoints, turn budget or successful input operation",
    )


def compare_scientific_documents(actual, retained, receipt, metadata, *, interpreter, prediction_image_id):
    """Check exact science while preserving a real model's received source text.

    Model spelling and line positions may differ. Canonical prepared values,
    binary32 inputs and ordered predictions must still equal the fixed oracle.
    """
    require(
        actual["job_ref"] == receipt["job_ref"] and actual["execution_attempt_ref"] == metadata["execution_attempt_ref"],
        "Scientific result Job/Attempt identity differs",
    )
    require(
        actual["input_fingerprint"] == receipt["specification_fingerprint"],
        "Scientific result is not bound to the submitted specification",
    )
    for key in ("selected_models", "runtime"):
        require(actual[key] == retained[key], f"Scientific {key} differs from the retained qualified receipt")
    require(len(actual["geometries"]) == len(retained["geometries"]) == 1, "Expected exactly one supplied geometry")
    require(
        actual["execution"]["image_id"] == prediction_image_id,
        "Prediction image differs from the admitted current deployment",
    )
    source = actual["geometries"][0]["source"]
    require(
        source["job_ref"] == receipt["job_ref"] and source["input_fingerprint"] == receipt["specification_fingerprint"],
        "Supplied-source provenance is not bound to the new Job/specification",
    )
    dynamic_source = ("job_ref", "input_fingerprint")
    if interpreter is not None:
        dynamic_source += ("sha256", "byte_length", "positions", "format")
    stable_source = {key: value for key, value in source.items() if key not in dynamic_source}
    retained_source = {key: value for key, value in retained["geometries"][0]["source"].items() if key not in dynamic_source}
    require(stable_source == retained_source, "Supplied source attribution differs from the retained receipt")
    preparation = retained["geometries"][0]["preparation"]
    if interpreter is not None:
        require(source["format"] in ("table", "xyz"), "Real interpretation did not select a supported pasted coordinate format")
        require(
            {"sha256", "byte_length", "positions"} <= source.keys(),
            "Real pasted source omitted its received-byte identity or line positions",
        )
        positions = source["positions"]
        require(
            type(source["sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is not None
            and type(source["byte_length"]) is int
            and source["byte_length"] > 0
            and type(positions) is list
            and len(positions) == len(actual["geometries"][0]["prepared_request"]["atoms"])
            and all(type(position) is int and position > 0 for position in positions)
            and all(left < right for left, right in pairwise(positions)),
            "Real pasted source did not retain its SHA-256, byte length and ordered received line positions",
        )
        preparation = preparation | {"method": {"table": "coordinate_table", "xyz": "xyz"}[source["format"]]}
        require_selected_interpretation(actual["interpretation"], interpreter, "prepare_pasted")
    require(actual["geometries"][0]["preparation"] == preparation, "Scientific source unit, method or conversion differs")
    for key in (
        "request_sha256",
        "prepared_request",
        "float32_coordinates",
        "prediction_unit",
        "loaded_elements",
        "predictions",
    ):
        require(
            actual["geometries"][0][key] == retained["geometries"][0][key],
            f"Scientific geometry {key} differs from the retained receipt",
        )


def compare_smiles_scientific_documents(actual, retained, receipt, metadata, *, interpreter, expected, prediction_image_id):
    """Check the admitted SMILES fixture; retained values supply metadata only.

    Native result admission owns complete graph, settings and stereochemistry.
    This fixture checks the authored case, observed byte identities and coverage.
    """
    require(
        actual["job_ref"] == receipt["job_ref"]
        and actual["execution_attempt_ref"] == metadata["execution_attempt_ref"]
        and actual["input_fingerprint"] == receipt["specification_fingerprint"],
        "Generated result Job, Attempt or specification identity differs",
    )
    for key in ("selected_models", "runtime"):
        require(actual[key] == retained[key], f"Generated scientific {key} differs from qualified metadata")
    require(actual["execution"]["image_id"] == prediction_image_id, "Generated prediction image differs")
    source = expected["smiles"].encode("utf-8")
    source_sha256 = hashlib.sha256(source).hexdigest()
    generation = actual["generation"]
    require(
        generation["selection"]
        == {
            "job_ref": receipt["job_ref"],
            "input_fingerprint": receipt["specification_fingerprint"],
            "origin": "interpreter_supplied_smiles",
            "byte_length": len(source),
            "source_sha256": source_sha256,
            "requested_count": expected["requested_conformers"],
        },
        "Generated selection differs from the received SMILES and requested count",
    )
    provenance = generation["provenance"]
    # smiles.py::generate_conformers freezes this observed metadata spelling.
    require(
        generation["provenance_sha256"]
        == hashlib.sha256(json.dumps(provenance, sort_keys=True, allow_nan=False).encode()).hexdigest(),
        "Observed generation provenance digest differs",
    )
    require(
        provenance["source_sha256"] == source_sha256
        and provenance["requested_count"] == expected["requested_conformers"]
        and [(atom["source_atom_index"], atom["element"]) for atom in provenance["source_atoms"]]
        == list(enumerate(expected["source_atoms"]))
        and provenance["added_hydrogen_parents"] == expected["expected_added_hydrogen_parents"],
        "Generated source atom order or added-hydrogen parents differ from the authored case",
    )
    require(len(actual["geometries"]) == expected["requested_conformers"], "Generated conformer count differs from the request")
    identifiers = set()
    hydrogen_count = len(expected["expected_added_hydrogen_parents"])
    atoms = expected["source_atoms"] + ["H"] * hydrogen_count
    for position, geometry in enumerate(actual["geometries"], start=1):
        source = geometry["source"]
        require(
            "geometry_id" in geometry
            and geometry["geometry_id"] == f"geometry-{position}"
            and source["kind"] == "smiles"
            and source["sha256"] == source_sha256
            and type(source["conformer_id"]) is int
            and source["conformer_id"] >= 0
            and source["conformer_id"] not in identifiers,
            f"Generated geometry {position}: source or unique actual conformer identity differs",
        )
        identifiers.add(source["conformer_id"])
        request = geometry["prepared_request"]
        coordinates = request["coordinates"]
        require(
            request["coordinate_unit"] == "angstrom"
            and request["atoms"] == atoms
            and {atom: atoms.count(atom) for atom in set(atoms)} == expected["generated_atom_counts"]
            and len(coordinates) == len(atoms)
            and all(
                len(row) == 3 and all(type(value) in (int, float) and math.isfinite(value) for value in row)
                for row in coordinates
            ),
            f"Generated geometry {position}: atom order, counts or finite angstrom coordinates differ",
        )
        # Independent spelling witness for input_geometry.py::coordinate_request;
        # generated coordinates and shifts have no numerical reference here.
        wire = {key: request[key] for key in ("coordinate_unit", "atoms", "coordinates")}
        require(
            geometry["request_sha256"] == hashlib.sha256(json.dumps(wire, separators=(",", ":")).encode("utf-8")).hexdigest()
            and geometry["float32_coordinates"]
            == [[struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in coordinates],
            f"Generated geometry {position}: executable request digest or actual binary32 coordinates differ",
        )
        require(
            geometry["preparation"]["method"] == "smiles"
            and geometry["preparation"]["coordinate_unit"] == "angstrom"
            and geometry["coverage"]
            == {
                "basis": "smiles_graph_expansion",
                "source_explicit_hydrogen_atoms": 0,
                "added_hydrogen_atoms": hydrogen_count,
                "coordinate_hydrogen_atoms": hydrogen_count,
                "predicted_hydrogen_atoms": hydrogen_count,
                "hydrogen_completeness": "not_independently_validated",
            },
            f"Generated geometry {position}: preparation or hydrogen coverage differs from the authored case",
        )
        preparation = geometry["preparation"]
        require(
            {"stereochemistry", "stereochemistry_sha256"} <= preparation.keys(),
            f"Generated geometry {position}: preparation omitted its observed stereochemistry or digest",
        )
        # smiles.py::_conformer freezes the observed annotation with this spelling;
        # matching its digest does not independently validate chemical stereochemistry.
        require(
            preparation["stereochemistry_sha256"]
            == hashlib.sha256(json.dumps(preparation["stereochemistry"], sort_keys=True).encode()).hexdigest(),
            f"Generated geometry {position}: observed stereochemistry digest differs",
        )
        predictions = geometry["predictions"]
        require(
            geometry["loaded_elements"] == ["C", "H"]
            and geometry["prediction_unit"] == "ppm"
            and [(row["atom_index"], row["element"]) for row in predictions]
            == [(index, atom) for index, atom in enumerate(atoms) if atom in ("C", "H")]
            and all(type(row["shift_ppm"]) in (int, float) and math.isfinite(row["shift_ppm"]) for row in predictions),
            f"Generated geometry {position}: C/H prediction rows are missing, reordered, nonfinite or include another element",
        )
    require_selected_interpretation(actual["interpretation"], interpreter, "prepare_smiles")


def compare_upload_scientific_documents(actual, retained, receipt, metadata, *, interpreter, upload, source, expected, prediction_image_id):
    """Compare existing XYZ values exactly while retaining the actual wire digest."""
    require(
        actual["job_ref"] == receipt["job_ref"]
        and actual["execution_attempt_ref"] == metadata["execution_attempt_ref"]
        and actual["input_fingerprint"] == receipt["specification_fingerprint"],
        "Upload result Job, Attempt or specification identity differs",
    )
    for key in ("selected_models", "runtime"):
        require(actual[key] == retained[key], f"Uploaded scientific {key} differs from the qualified receipt")
    require(
        actual["execution"]["image_id"] == prediction_image_id and len(actual["geometries"]) == 1,
        "Uploaded prediction image or geometry count differs",
    )
    geometry = actual["geometries"][0]
    digest = hashlib.sha256(source).hexdigest()
    require(
        upload["byte_length"] == len(source)
        and upload["content_hash"] == "sha256:" + digest
        and geometry["source"]
        == {
            "sha256": digest,
            "record_number": 1,
            "position_kind": "line_number",
            "positions": [3, 4, 5, 6, 7],
            "upload_ref": upload["upload_ref"],
            "byte_length": len(source),
        },
        "Uploaded XYZ byte identity, membership reference or source positions differ",
    )
    request = geometry["prepared_request"]
    require(
        request == {"coordinate_unit": "angstrom", "atoms": expected["atoms"], "coordinates": expected["coordinates_angstrom"]},
        "Uploaded XYZ prepared values differ from the independent authored angstrom oracle",
    )
    # input_geometry.py::coordinate_request spells actual admitted values; 0.0
    # and authored integer 0 compare numerically but have different wire bytes.
    wire = {key: request[key] for key in ("coordinate_unit", "atoms", "coordinates")}
    require(
        geometry["request_sha256"] == hashlib.sha256(json.dumps(wire, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "Uploaded actual request byte digest differs",
    )
    coordinates = [[struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in expected["coordinates_angstrom"]]
    require(
        geometry["float32_coordinates"] == coordinates == retained["geometries"][0]["float32_coordinates"],
        "Uploaded binary32 coordinates differ from authored or retained inputs",
    )
    require(
        geometry["preparation"]
        == {"method": "xyz", "source_unit": "angstrom", "scale_to_angstrom": 1.0, "conversion_reference": "identity (angstrom)"},
        "Uploaded XYZ preparation facts differ",
    )
    for key in ("prediction_unit", "loaded_elements", "predictions", "coverage"):
        require(
            key in geometry and geometry[key] == retained["geometries"][0][key],
            f"Uploaded scientific {key} differs from the pinned predictions",
        )
    require_selected_interpretation(actual["interpretation"], interpreter, "prepare_upload")


def compare_sdf_scientific_documents(actual, retained, receipt, metadata, *, interpreter, upload, prediction_image_id):
    """Compare supplied SDF science across new API Job and Upload identities.

    R46 pins the native reference; R48 owns member-versus-archive provenance.
    This compares exact science, not independent accuracy or GUI behavior.
    The comparison was ported from compareSubmittedScience at:
    https://github.com/numpde/nmrnet-repro/blob/2efc810/tests/integration/provider_runtime/gui_science.mjs
    """
    require(
        actual["schema_id"] == "nmrnet.coordinate_shifts.result.v2"
        and actual["job_ref"] == receipt["job_ref"]
        and actual["execution_attempt_ref"] == metadata["execution_attempt_ref"]
        and actual["input_fingerprint"] == receipt["specification_fingerprint"],
        "SDF result Job, Attempt or specification identity differs",
    )
    require(
        actual["job_ref"] != retained["job_ref"] and actual["execution_attempt_ref"] != retained["execution_attempt_ref"],
        "A new SDF Job reused the reference identities",
    )
    for key in ("selected_models", "runtime"):
        require(actual[key] == retained[key], f"SDF scientific {key} differs from R46")
    require(
        actual["execution"]["image_id"] == prediction_image_id
        and len(actual["geometries"]) == len(retained["geometries"]) == 2,
        "SDF prediction image or two-record population differs",
    )
    archive = {
        "sha256": upload["content_hash"].removeprefix("sha256:"),
        "byte_length": upload["byte_length"],
        "member_name": "butane_conformers.sdf",
    }
    for actual_geometry, original in zip(actual["geometries"], retained["geometries"], strict=True):
        require(
            actual_geometry["source"] == original["source"] | {"upload_ref": upload["upload_ref"], "archive": archive},
            "SDF original member, record order or outer Upload identity differs",
        )
        for key in (
            "geometry_id",
            "request_sha256",
            "prepared_request",
            "float32_coordinates",
            "preparation",
            "prediction_unit",
            "loaded_elements",
            "predictions",
            "coverage",
        ):
            require(actual_geometry[key] == original[key], f"SDF scientific {key} differs from R46")
    require_selected_interpretation(actual["interpretation"], interpreter, "prepare_upload")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--readback-seconds", required=True, type=int)
    parser.add_argument("--process-seconds", required=True, type=int)
    parser.add_argument("--case-kind", required=True, choices=("pasted", "smiles", "smiles-multiple", "upload", "sdf-zip"))
    parser.add_argument("--job-receipt", type=Path, help="Read back this original Job only; never create, select or open a Job")
    parser.add_argument(
        "--upload-receipt",
        type=Path,
        help="Original SDF ZIP reservation after explicit storage-owner reconciliation; no new Upload is created",
    )
    args = parser.parse_args()
    require(
        0 < args.readback_seconds < args.process_seconds,
        "Qualification requires a positive readback budget shorter than its whole-process budget",
    )

    # The owned whole-process deadline also interrupts a peer that keeps a body
    # alive by trickling bytes inside its socket's inactivity timeout.
    def deadline_reached(_signum, _frame):
        raise TimeoutError(
            f"Actual API qualification exceeded {args.process_seconds} seconds; "
            "inspect retained requests and receipts before any new mutation"
        )

    signal.signal(signal.SIGALRM, deadline_reached)
    signal.alarm(args.process_seconds)
    require(
        str(ipaddress.IPv4Address(args.address)) == args.address,
        "Supply the observed publication's literal canonical IPv4 address",
    )
    os.umask(0o077)
    output = Path("/output")
    require(not any(output.iterdir()), "Use fresh private client output; an uncertain mutation must not be resubmitted")
    artifacts = {name: (Path("/credentials") / name).read_bytes() for name in ("registration.json", "signing.private.json")}
    secret = Path("/credentials/signing.private.json").stat()
    require(
        secret.st_uid == os.geteuid() and secret.st_mode & 0o777 in (0o400, 0o600),
        "Client credential must be caller-owned and mode0400 or0600",
    )
    identity = json.loads(Path("/input/identity.json").read_bytes())
    user = UserAuthorityRefs(user_id=args.user_id)
    project_ref = require_project_ref("project_ref", args.project_ref)
    validate_user_credential_artifacts(artifacts=artifacts, profile=identity["profile"], user_id=user.user_id)
    case = json.loads(Path("/fixture/case.json").read_bytes())
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", identity["prediction_image_id"]) is not None, "Current prediction image pin is malformed")
    if args.case_kind == "sdf-zip":
        _retained_sdf_science()
    else:
        _retained_science()
    with tempfile.TemporaryDirectory() as scratch:
        frozen = Path(scratch) / "signing.private.json"
        frozen.write_bytes(artifacts["signing.private.json"])
        client = Client(output, frozen, args.address, args.interface, identity, project_ref, user)
        if args.job_receipt is None:
            receipt = submit(client, case, case_kind=args.case_kind, upload_receipt=args.upload_receipt)
        else:
            with args.job_receipt.open("rb") as stream:
                raw = stream.read(65537)
            require(len(raw) <= 65536, "Original Job receipt exceeds the readback ceiling")
            receipt = decode_strict_json(raw.decode("utf-8"))
            require(isinstance(receipt, dict) and receipt.get("project_ref") == project_ref,
                    "Original Job receipt must name the selected project")
            require_job_ref("Original Job receipt job_ref", receipt.get("job_ref"))
        receive(client, receipt, readback_seconds=args.readback_seconds, case_kind=args.case_kind, prediction_image_id=identity["prediction_image_id"])
        (output / "passed.json").write_text(
            json.dumps(
                {
                    "job_ref": receipt["job_ref"],
                    "scientific_repeatability": args.case_kind in ("pasted", "upload", "sdf-zip"),
                    "interpretation_scripted": identity["interpretation_scripted"],
                    "requests": client.count,
                }
            )
            + "\n"
        )
    signal.alarm(0)


if __name__ == "__main__":
    main()
