"""One actual API Job through the API-owned client contract.

Uses the installed nmr-api-v1 user-artifact validator, signing recipe, Job-create
canonicalizer and result decoders (e7790cf6). No mutation is retried, redirected
or reconstructed after dispatch. Raw bodies and receipts remain in private output;
keys/signatures/transcripts do not. The caller selects scripted or real pasted
interpretation. Pasted input retains its fixed numerical oracle; CCl generation
checks observed provenance and H/C coverage without a numerical golden result.
Neither qualifies GUI behavior or experimental accuracy.
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
from contextlib import redirect_stdout
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

from nmr_api.canonical_base64 import decode_canonical_base64
from nmr_api.canonical_json import decode_strict_json
from nmr_api.discovery_examples.sign_request import main as sign_request
from nmr_api.public.job_create_request import canonicalize_job_create_request_body
from nmr_api.security.project_refs import require_project_ref
from nmr_api.security.user_credential_artifacts import validate_user_credential_artifacts
from nmr_api.security.user_ids import UserAuthorityRefs

from tools.bound_http_proxy import connect_bound


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class Client:
    """Freshly sign each finite request; preserve received bytes before admission."""

    def __init__(self, output, credential, address, interface, identity, project_ref, user):
        self.output, self.credential, self.count = output, credential, 0
        self.address, self.interface = address, interface
        self.identity, self.project_ref, self.user = identity, project_ref, user
        origin = urlsplit(identity["origin"])
        self.host, self.port = origin.hostname, origin.port if origin.port is not None else 443
        self.trust = ssl.create_default_context(cafile="/input/api-ca.crt")

    def request(self, label, method, path, query="", body=None, status=200):
        self.count += 1
        prefix = self.output / f"{self.count:03}-{label}"
        signing = [str(self.credential), method, self.identity["authority"], path, query]
        if body is not None:
            with Path(str(prefix) + ".request.json").open("xb") as stream:
                stream.write(body)
            signing.append(str(prefix) + ".request.json")
        headers = io.StringIO()
        with redirect_stdout(headers):
            sign_request(signing)
        admitted_headers = dict(line.split(": ", 1) for line in headers.getvalue().splitlines())
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=10, context=self.trust)
        try:
            # Use the same existing bound-address owner as the provider's mapped
            # proxy. TLS and HTTP still use the original canonical authority.
            outbound = connect_bound(self.address, self.port, self.interface)
            outbound.settimeout(10)
            connection.sock = self.trust.wrap_socket(outbound, server_hostname=self.host)
            connection.request(method, path + ("?" + query if query else ""), body=body, headers=admitted_headers)
            response = connection.getresponse()
            content = response.read(1048577)
            Path(str(prefix) + ".response.json").write_bytes(content)
            topology = response.getheader("Nmr-Api-Topology")
            Path(str(prefix) + ".head.json").write_text(json.dumps({"status": response.status, "topology": topology}) + "\n")
            require(
                response.status == status,
                f"{label}: expected HTTP {status}, received {response.status}; inspect retained response",
            )
            require(topology == self.identity["topology"], f"{label}: the API did not identify the selected topology")
            require(len(content) <= 1048576, f"{label}: response exceeds the qualification byte ceiling")
            return decode_strict_json(content.decode("utf-8"))
        finally:
            connection.close()


def submit(client, case):
    directory = client.request(
        "directory", "GET", "/v1/providers", f"project_ref={client.project_ref}&analysis_kind_ref=peaks_from_mol&limit=100"
    )
    require(
        any(item["provider_ref"] == client.identity["provider_ref"] for item in directory["providers"]),
        "The dedicated NMRNet offering was not discoverable",
    )
    body = json.dumps(
        {
            "operation_ref": "nmrnet-first-api:" + uuid.uuid4().hex,
            "project_ref": client.project_ref,
            "analysis_kind_ref": "peaks_from_mol",
            "description": "NMRNet chloromethane; "
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


def receive(client, receipt, *, readback_seconds, case_kind):
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
                case_kind=case_kind,
            )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "The received result exceeded the qualification readback deadline; it was retained and no mutation was retried"
                )
            return
        time.sleep(1)
    raise TimeoutError("The bounded prediction readback did not receive a result; no mutation was retried")


def compare_science(content, receipt, metadata, *, interpreter, case_kind):
    """Use the pinned independent receipt; this run never creates its own oracle."""
    retained_bytes = Path("/input/retained-result.json").read_bytes()
    require(
        hashlib.sha256(retained_bytes).hexdigest() == "9ae5017c42af5c5691554eb3ff6dced7895c6a00ab177975d8aa15ae0c58f59a",
        "The independent retained scientific receipt differs",
    )
    if case_kind == "pasted":
        compare_scientific_documents(json.loads(content), json.loads(retained_bytes), receipt, metadata, interpreter=interpreter)
    else:
        expected = next(
            case
            for case in json.loads(Path("/fixture/smiles.json").read_bytes())["normative"]
            if case["name"] == "implicit_hydrogens"
        )
        compare_smiles_scientific_documents(
            json.loads(content), json.loads(retained_bytes), receipt, metadata, interpreter=interpreter, expected=expected
        )


def compare_scientific_documents(actual, retained, receipt, metadata, *, interpreter):
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
        actual["execution"]["image_id"] == retained["execution"]["image_id"],
        "Prediction image differs from the retained scientific receipt",
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
        observation = actual["interpretation"]
        require(
            observation["schema_id"] == "nmrnet.input_interpretation.v1"
            and observation["outcome"] == "selected"
            and observation["operations"] == ["prepare_pasted"]
            and observation["turns_started"] == 1
            and observation["corrections"] == 0
            and observation["routes"] == [{"endpoint": interpreter, "turns_started": 1}],
            "Real interpretation did not select pasted input in one uncorrected exchange with the admitted endpoint",
        )
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


def compare_smiles_scientific_documents(actual, retained, receipt, metadata, *, interpreter, expected):
    """Check CCl/N1 generation facts; retained pasted values are not a golden.

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
    require(actual["execution"]["image_id"] == retained["execution"]["image_id"], "Generated prediction image differs")
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
        "Generated selection differs from the received CCl/N1 case",
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
        "Generated source atom order or added-hydrogen parents differ from the authored CCl case",
    )
    require(len(actual["geometries"]) == expected["requested_conformers"] == 1, "Expected exactly one generated conformer")
    geometry = actual["geometries"][0]
    require(
        geometry["source"]["kind"] == "smiles"
        and geometry["source"]["sha256"] == source_sha256
        and type(geometry["source"]["conformer_id"]) is int
        and geometry["source"]["conformer_id"] >= 0,
        "Generated geometry source or actual conformer identity differs",
    )
    request = geometry["prepared_request"]
    atoms = expected["source_atoms"] + ["H"] * len(expected["expected_added_hydrogen_parents"])
    coordinates = request["coordinates"]
    require(
        request["coordinate_unit"] == "angstrom"
        and request["atoms"] == atoms
        and {atom: atoms.count(atom) for atom in set(atoms)} == expected["generated_atom_counts"]
        and len(coordinates) == len(atoms)
        and all(
            len(row) == 3 and all(type(value) in (int, float) and math.isfinite(value) for value in row) for row in coordinates
        ),
        "Generated CCl atom order, counts or finite angstrom coordinates differ",
    )
    # Independent spelling witness for input_geometry.py::coordinate_request;
    # neither coordinates nor shifts are compared with the pasted/N3 fixture.
    wire = {key: request[key] for key in ("coordinate_unit", "atoms", "coordinates")}
    require(
        geometry["request_sha256"] == hashlib.sha256(json.dumps(wire, separators=(",", ":")).encode("utf-8")).hexdigest()
        and geometry["float32_coordinates"]
        == [[struct.unpack("f", struct.pack("f", value))[0] for value in row] for row in coordinates],
        "Generated executable request digest or actual binary32 coordinates differ",
    )
    require(
        geometry["preparation"]["method"] == "smiles"
        and geometry["preparation"]["coordinate_unit"] == "angstrom"
        and geometry["coverage"]
        == {
            "basis": "smiles_graph_expansion",
            "source_explicit_hydrogen_atoms": 0,
            "added_hydrogen_atoms": 3,
            "coordinate_hydrogen_atoms": 3,
            "predicted_hydrogen_atoms": 3,
            "hydrogen_completeness": "not_independently_validated",
        },
        "Generated preparation or hydrogen coverage differs from the CCl case",
    )
    preparation = geometry["preparation"]
    require(
        {"stereochemistry", "stereochemistry_sha256"} <= preparation.keys(),
        "Generated preparation omitted its observed stereochemistry or digest",
    )
    # smiles.py::_conformer freezes the observed annotation with this spelling;
    # matching its digest does not independently validate chemical stereochemistry.
    require(
        preparation["stereochemistry_sha256"]
        == hashlib.sha256(json.dumps(preparation["stereochemistry"], sort_keys=True).encode()).hexdigest(),
        "Observed generated stereochemistry digest differs",
    )
    predictions = geometry["predictions"]
    require(
        geometry["loaded_elements"] == ["C", "H"]
        and geometry["prediction_unit"] == "ppm"
        and [(row["atom_index"], row["element"]) for row in predictions] == [(0, "C"), (2, "H"), (3, "H"), (4, "H")]
        and all(type(row["shift_ppm"]) in (int, float) and math.isfinite(row["shift_ppm"]) for row in predictions),
        "Generated result must predict every C/H atom in order with finite ppm values and no chlorine shift",
    )
    observation = actual["interpretation"]
    require(
        observation["schema_id"] == "nmrnet.input_interpretation.v1"
        and observation["outcome"] == "selected"
        and observation["operations"] == ["prepare_smiles"]
        and observation["turns_started"] == 1
        and observation["corrections"] == 0
        and observation["routes"] == [{"endpoint": interpreter, "turns_started": 1}],
        "Real interpretation did not select SMILES in one uncorrected exchange with the admitted endpoint",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--readback-seconds", required=True, type=int)
    parser.add_argument("--process-seconds", required=True, type=int)
    parser.add_argument("--case-kind", required=True, choices=("pasted", "smiles"))
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
    with tempfile.TemporaryDirectory() as scratch:
        frozen = Path(scratch) / "signing.private.json"
        frozen.write_bytes(artifacts["signing.private.json"])
        client = Client(output, frozen, args.address, args.interface, identity, project_ref, user)
        receipt = submit(client, case)
        receive(client, receipt, readback_seconds=args.readback_seconds, case_kind=args.case_kind)
        (output / "passed.json").write_text(
            json.dumps(
                {
                    "job_ref": receipt["job_ref"],
                    "scientific_repeatability": args.case_kind == "pasted",
                    "interpretation_scripted": identity["interpretation_scripted"],
                    "gui_exercised": False,
                    "requests": client.count,
                }
            )
            + "\n"
        )
    signal.alarm(0)


if __name__ == "__main__":
    main()
