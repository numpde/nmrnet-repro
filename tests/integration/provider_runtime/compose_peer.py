"""Disposable signed peers for the final Compose path, never a provider entrypoint.

Reuse ProcessPeers' signed API and RuntimePeers' scripted interpretation, with
the fixed independent coordinate/unit oracle. Shift checks here cover
population and finiteness for pasted and generated cases. Reference and capacity cases reuse
their existing independent owners. Scripted interpretation is not a live model
or deployed API witness.
"""

from tools.container_guard import require_container

require_container()

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import shutil
import struct
import tomllib
import sys
from decimal import Decimal
from dataclasses import asdict, replace
from fractions import Fraction
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit

from binding_inputs import write_deployment_inputs


def successive_api_reply(peer, path, body, completed, inputs):
    """Two sequential Jobs with distinct wire identities, using existing fixtures."""
    from tests.fakes.attempts import complete_response, observation, progress_response, start_response
    from tests.fakes.job_feed import page
    from tests.fakes.job_input import response
    from nmrnet_provider.config import ANALYSIS_KIND_REF

    if path == "/provider/v1/jobs":
        document = page()
        jobs = [] if completed == 2 else [{**asdict(inputs[completed].identity),
            "analysis_kind_ref": ANALYSIS_KIND_REF, "created_at": "2026-10-18T00:00:00.123456Z"}]
        document.update(analysis_kind_ref=ANALYSIS_KIND_REF, jobs=jobs, next_cursor=None)
        return json.dumps(document).encode()
    if completed >= 2:
        raise AssertionError("Successive witness received another scientific operation after both completions")
    current = inputs[completed]
    job = current.identity.job_ref
    attempt = "execution_attempt:sha256:" + ("a" if completed == 0 else "c") * 64
    if path == f"/provider/v1/jobs/{job}/input":
        return response(current.specification_bytes, job_ref=job)
    if path == "/provider/v1/execution-attempts/start":
        if json.loads(body)["job_ref"] != job:
            raise AssertionError("Successive start changed the admitted Job")
        return start_response(job_ref=job, execution_attempt_ref=attempt, analysis_kind_ref=ANALYSIS_KIND_REF)
    if path == f"/provider/v1/execution-attempts/{attempt}":
        return observation(job_ref=job, execution_attempt_ref=attempt)
    if path == f"/provider/v1/execution-attempts/{attempt}/progress":
        return progress_response(execution_attempt_ref=attempt, phase=json.loads(body)["phase"])
    if path == "/provider/v1/execution-attempts/complete":
        declaration = json.loads(body)
        if declaration["execution_attempt_ref"] != attempt:
            raise AssertionError("Successive completion changed its Attempt")
        content = base64.b64decode(declaration["canonical_result_base64"], validate=True)
        peer.result = json.loads(content)
        if (peer.result["job_ref"], peer.result["execution_attempt_ref"]) != (job, attempt):
            raise AssertionError("Successive result changed its Job or Attempt association")
        return complete_response(execution_attempt_ref=attempt,
            analysis_result_ref="analysis_result:sha256:" + ("b" if completed == 0 else "d") * 64,
            result_schema_id=peer.result["schema_id"], result_byte_length=len(content),
            result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest())
    raise AssertionError("Successive witness received an unexpected Job/Attempt route")


def require_exported_result(result, case, manifest):
    from tests.integration.nmrnet.reference_fixtures import PRODUCTION_SHA256, REFERENCE_SHA256, pinned_packet
    from tests.integration.nmrnet.reference_fixtures import require_reference_predictions

    geometries = result["geometries"]
    for geometry, record in zip(geometries, manifest["records"], strict=True):
        if geometry["prepared_request"] != record["original_request"]:
            raise AssertionError("Connected science changed the original atoms, coordinates or units")
        if (geometry["source"]["record_number"], geometry["source"]["positions"]) != (
            record["source_record_number"], record["source_positions"]
        ):
            raise AssertionError("Connected science changed original record/atom correspondence")
        if geometry["source"]["sha256"] != manifest["source"]["sha256"]:
            raise AssertionError("Connected science did not consume the pinned original Upload bytes")
    if case == "references":
        reference = pinned_packet("/fixture/reference-cpu-float32.json", REFERENCE_SHA256)
        production = pinned_packet("/fixture/prediction-cpu-float32.json", PRODUCTION_SHA256)
        return require_reference_predictions(manifest["records"], [item["predictions"] for item in geometries], reference, production)
    for element, geometry in zip(("H", "C"), geometries, strict=True):
        predictions = geometry["predictions"]
        if [(item["atom_index"], item["element"]) for item in predictions] != [(index, element) for index in range(128)]:
            raise AssertionError("Connected capacity output must contain every original H128/C128 index")
        if not all(math.isfinite(item["shift_ppm"]) for item in predictions):
            raise AssertionError("Connected capacity output contains a nonfinite shift")
    return {"scope": "synthetic input/output capacity, not chemical accuracy", "atoms_per_geometry": 128,
            "predictions_by_nucleus": {"H": 128, "C": 128}}


def require_generated_result(result, fixture):
    """Check the authored CCl graph and two actual conformers, without a shift oracle.

    The source/parent facts are from preparation/smiles.json::implicit_hydrogens;
    this connection asks for two conformers rather than that fixture's one.
    Native admission owns the complete generation and model contracts.
    """
    expected = next(item for item in fixture["normative"] if item["name"] == "implicit_hydrogens")
    digest = hashlib.sha256(expected["smiles"].encode()).hexdigest()
    selection = result["generation"]["selection"]
    if selection != {"job_ref": result["job_ref"], "input_fingerprint": result["input_fingerprint"],
                     "origin": "interpreter_supplied_smiles", "source_sha256": digest,
                     "byte_length": len(expected["smiles"].encode()), "requested_count": 2}:
        raise AssertionError("Generated selection differs from the received CCl/count arguments")
    provenance = result["generation"]["provenance"]
    if ([(atom["source_atom_index"], atom["element"]) for atom in provenance["source_atoms"]]
            != list(enumerate(expected["source_atoms"]))
            or provenance["added_hydrogen_parents"] != expected["expected_added_hydrogen_parents"]):
        raise AssertionError("Generated source order or added-hydrogen parents differ from the authored fixture")
    geometries = result["geometries"]
    if len(geometries) != 2:
        raise AssertionError("Generated result must contain both requested conformers")
    identifiers = set()
    atoms = expected["source_atoms"] + ["H"] * len(expected["expected_added_hydrogen_parents"])
    for geometry in geometries:
        source = geometry["source"]
        identifier = source["conformer_id"]
        if (source["kind"] != "smiles" or source["sha256"] != digest or type(identifier) is not int
                or identifier < 0 or identifier in identifiers):
            raise AssertionError("Generated conformer source/identity differs or repeats")
        identifiers.add(identifier)
        request = geometry["prepared_request"]
        coordinates = request["coordinates"]
        if (request["atoms"] != atoms or request["coordinate_unit"] != "angstrom"
                or len(coordinates) != len(atoms)
                or not all(len(row) == 3 and all(math.isfinite(value) for value in row) for row in coordinates)):
            raise AssertionError("Generated atoms or finite angstrom coordinates differ")
        wire = {key: request[key] for key in ("coordinate_unit", "atoms", "coordinates")}
        if (geometry["request_sha256"] != hashlib.sha256(json.dumps(wire, separators=(",", ":")).encode()).hexdigest()
                or geometry["float32_coordinates"] != [[struct.unpack("f", struct.pack("f", value))[0]
                                                        for value in row] for row in coordinates]):
            raise AssertionError("Generated executable bytes or binary32 projection differ")
        predictions = geometry["predictions"]
        if ([(item["atom_index"], item["element"]) for item in predictions]
                != [(0, "C"), (2, "H"), (3, "H"), (4, "H")]
                or not all(math.isfinite(item["shift_ppm"]) for item in predictions)):
            raise AssertionError("Generated H/C population differs or contains nonfinite shifts")
    return {"molecule": expected["smiles"], "conformers": 2, "predictions": 8,
            "scope": "authored graph, source/parent identities, finite coordinates and shifts; no accuracy claim"}


async def serve(root, seconds, case, delivery_project):
    # Match the installed-consumer fixture layout. These are
    # explicit read-only qualification inputs; production images import no tests.
    fixture = Path("/tmp/compose-peer")
    shutil.copytree("/fixture/tests", fixture / "tests")
    shutil.copytree("/fixture/binding-tests", fixture / "tests/integration/nmrnet")
    sys.path.insert(0, str(fixture))
    from tests.fakes.api_tls import api_tls_peer
    from tests.fakes.job_feed import page
    from tests.fakes.job_input import facts, response
    from tests.fakes.tls_peer import TlsPeer
    from tests.fakes.acquisition import store_headers
    from tests.fakes.uploads import UPLOAD_REF
    from tests.integration.nmrnet.config_ports import ConfigFiles
    from tests.integration.nmrnet.reference_fixtures import NM_TABLE, nm_conversion_oracle
    from tests.integration.nmrnet.process_ports import ProcessPeers
    from tests.integration.nmrnet.runtime_ports import RuntimePeers, require_api_credentials, require_download_credentials, require_model_credentials
    from tests.integration.nmrnet.interpreter_ports import tool_reply
    from tests.integration.nmrnet.scientific_inputs import load_scientific_inputs
    from tests.integration.nmrnet.runner_crash_peer import reader_crash_witness
    from nmrnet_provider.config import ANALYSIS_KIND_REF, load_provider_config
    from provider_core.job_input import verify_job_input

    os.umask(0o077)
    peer = ProcessPeers()
    direct_case = case in ("pasted", "smiles", "successive", "recovery")
    expected_jobs = {"successive": 2, "recovery": 0}.get(case, 1)
    peer.output = {"verbosity": "max"}
    if case == "references":
        # Seventeen digits preserve a binary64 scalar's round trip. Compare the
        # original unrounded reference values, without changing product defaults
        # or weakening the existing exact population comparison.
        peer.output["shift_significant_digits"] = 17
    if case == "smiles":
        peer.route = "smiles"
        instructions = b"Predict CCl from SMILES, generating two conformers."
        expected_model_calls = 1
        maximum_records = 1
    elif direct_case:
        peer.route = "pasted"
        peer.ports.source_text = NM_TABLE.decode()
        instructions = NM_TABLE
        expected_model_calls = expected_jobs
        maximum_records = 1
    else:
        peer.route = "upload"
        peer.ports.source, manifest = load_scientific_inputs(Path("/fixture/scientific_inputs"), case)
        maximum_records = len(manifest["records"])
        instructions = f"Predict Upload {UPLOAD_REF}, all {maximum_records} records, in angstroms.".encode()
        expected_model_calls = 2
    peer.ports.instructions = verify_job_input(response(instructions), feed=facts(instructions))
    successive_inputs = tuple(verify_job_input(response(instructions, job_ref=job),
        feed=replace(facts(instructions), job_ref=job)) for job in ("job:compose-first", "job:compose-second")) if case == "successive" else ()
    calls = {"model": 0, "start": 0, "complete": 0, "hello": 0, "download": 0}
    discoveries = 0
    oracle_bytes = Path("/fixture/expected.json").read_bytes()
    expected = json.loads(oracle_bytes, parse_float=Decimal)["geometries"]["chloromethane"]

    def publish(name, value):
        publish_bytes(name, (json.dumps(value, sort_keys=True) + "\n").encode())

    def publish_bytes(name, content):
        pending = root / ("." + name + ".pending")
        with pending.open("xb") as stream:
            stream.write(content)
        pending.replace(root / name)

    async def model_reply(request, body):
        calls["model"] += 1
        if calls["model"] > expected_model_calls:
            raise AssertionError("The connected witness must not repeat successful interpretation")
        if not direct_case:
            require_model_credentials(request)
            if json.loads(body)["messages"][-1]["role"] != "tool":
                return tool_reply("inspect", "inspect_uploads", {})
            return tool_reply("prepare", "prepare_upload", {
                "upload_ref": UPLOAD_REF, "source_format": "xyz", "source_unit": "angstrom",
                "selection": list(range(1, maximum_records + 1)), "alternate_location": None, "output": peer.output,
            })
        return await RuntimePeers.model_reply(peer, request, body)

    async def store_reply(request, body):
        require_download_credentials(request, body)
        calls["download"] += 1
        if calls["download"] != 1:
            raise AssertionError("The connected witness must acquire its selected Upload once")
        return peer.ports.source

    async def api_reply(request, body):
        nonlocal discoveries
        require_api_credentials(request)
        path = urlsplit(request.split(b"\r\n", 1)[0].decode().split()[1]).path
        if path.endswith("/hello"):
            calls["hello"] += 1
        if path.endswith("/start"):
            calls["start"] += 1
            if calls["start"] > expected_jobs:
                raise AssertionError("The connected witness must not create another Attempt")
        if path == "/provider/v1/jobs" and (
            case == "recovery" or calls["start"] > calls["complete"] or calls["complete"] >= expected_jobs
        ):
            # A claimed Job is not offered again while its result is published.
            from tests.fakes.api_tls import verify_signed_request

            verify_signed_request(request, body, origin=peer.peer.origin, key=peer.config.api.identity.private_key)
            if case == "recovery":
                discoveries += 1
                publish("discoveries.json", {"count": discoveries})
            empty = page()
            empty.update(analysis_kind_ref=ANALYSIS_KIND_REF, jobs=[], next_cursor=None)
            return json.dumps(empty).encode()
        if case == "successive" and path not in ("/provider/v1/hello", "/provider/v1/execution-attempts"):
            from tests.fakes.api_tls import verify_signed_request

            verify_signed_request(request, body, origin=peer.peer.origin, key=peer.config.api.identity.private_key)
            result = successive_api_reply(peer, path, body, calls["complete"], successive_inputs)
        elif path.endswith("/read-capability"):
            from tests.fakes.api_tls import verify_signed_request

            verify_signed_request(request, None, origin=peer.peer.origin, key=peer.config.api.identity.private_key)
            result = await RuntimePeers.api_reply(peer, request, body)
        else:
            result = await ProcessPeers.reply(peer, request, body)
        if path.endswith("/complete"):
            calls["complete"] += 1
            if calls["complete"] > expected_jobs:
                raise AssertionError("The no-outage witness must publish its result once")
            if case == "smiles":
                fixture_bytes = Path("/fixture/smiles.json").read_bytes()
                proof = require_generated_result(peer.result, json.loads(fixture_bytes))
                publish("result.json", peer.result)
                publish("completed.json", {"calls": dict(calls), "case": case, "scientific_proof": proof,
                                           "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
                                           "result_sha256": hashlib.sha256((root / "result.json").read_bytes()).hexdigest()})
                return result
            if not direct_case:
                scientific_proof = require_exported_result(peer.result, case, manifest)
                publish("result.json", peer.result)
                publish("completed.json", {"calls": calls, "case": case, "scientific_proof": scientific_proof,
                                           "historical_input_manifest_sha256": hashlib.sha256(
                                               (Path("/fixture/scientific_inputs") / (case + ".manifest.json")).read_bytes()).hexdigest(),
                                           "result_sha256": hashlib.sha256((root / "result.json").read_bytes()).hexdigest()})
                return result
            geometry, = peer.result["geometries"]
            prepared = geometry["prepared_request"]
            if prepared["atoms"] != expected["atoms"] or prepared["coordinate_unit"] != "angstrom":
                raise AssertionError("Connected result changed supplied atom order")
            source_rows = [line.split()[1:] for line in NM_TABLE.decode().splitlines()]
            for source, target, actual in zip(source_rows, expected["coordinates_angstrom"], prepared["coordinates"], strict=True):
                for original_nm, independent_angstrom, observed in zip(source, target, actual, strict=True):
                    intended, bound = nm_conversion_oracle(original_nm, independent_angstrom)
                    if abs(Fraction.from_float(observed) - intended) > bound:
                        raise AssertionError("Connected coordinate conversion exceeds the independent binary64 rounding bound")
            if [(item["atom_index"], item["element"]) for item in geometry["predictions"]] != [(0, "H"), (2, "C"), (3, "H"), (4, "H")]:
                raise AssertionError("Connected result changed the H/C prediction population")
            if not all(math.isfinite(item["shift_ppm"]) for item in geometry["predictions"]):
                raise AssertionError("Connected prediction contains a nonfinite shift")
            publish("result.json", peer.result)
            completion = {"calls": dict(calls), "coordinate_oracle_sha256": hashlib.sha256(oracle_bytes).hexdigest(),
                          "result_sha256": hashlib.sha256((root / "result.json").read_bytes()).hexdigest()}
            if case == "successive":
                publish(f"result-{calls['complete']}.json", peer.result)
                publish(f"completed-{calls['complete']}.json", completion)
            if calls["complete"] == expected_jobs:
                publish("completed.json", completion)
        return result

    async with asyncio.timeout(seconds), AsyncExitStack() as peers:
        model = await peers.enter_async_context(TlsPeer("success", response_factory=model_reply))
        peer.model_peer = model
        api = await peers.enter_async_context(api_tls_peer("success", response_factory=api_reply))
        if direct_case:
            store = api
        else:
            headers = tuple((name, value) for name, value in store_headers(peer.ports.source) if name != b"content-length")
            store = await peers.enter_async_context(TlsPeer("success", response_factory=store_reply, response_headers=headers))
            peer.store_peer = store
        peer.peer = api
        source = root / "configuration"
        source.mkdir(mode=0o700)
        for name, owner in (("api-ca.pem", api), ("model-ca.pem", model), ("store-ca.pem", store)):
            (source / name).write_bytes((Path(owner.scratch.name) / "cert.pem").read_bytes())
        files = ConfigFiles(source, api_origin=api.origin, model_origin=model.origin, store_origin=store.origin,
                            maximum_live_attempts=2, ca_file=source / "api-ca.pem",
                            model_ca_file=source / "model-ca.pem", store_ca_file=source / "store-ca.pem")
        peer.config = load_provider_config(files.path)
        raw = files.raw.replace(str(files.state).encode(), b"/srv/nmrnet/attempts")
        raw = raw.replace(str(source).encode(), b"/srv/nmrnet/input")
        route = {"mode": "mapped", "interface": "lo", "log_level": "INFO",
                 "addresses": ["127.0.0.1=127.0.0.1"]}
        runtime = tomllib.loads(raw.decode())
        actual_api = tomllib.loads(files.raw.decode())["api"]
        write_deployment_inputs(
            root, runtime, api=actual_api, api_route=route,
            routes={"interpreter": route, "store": route}, project=delivery_project,
            maximum_startup_bytes=4194304, resolvers=[], shared_fixture=True,
        )
        publish("ready.json", {"api_origin": api.origin, "model_origin": model.origin,
                               "provider_ref": actual_api["provider_ref"]})
        crash = None
        while not (root / "done").exists():
            if case == "recovery" and crash is None and (root / "reader-crash-start").exists():
                crash = asyncio.create_task(asyncio.to_thread(reader_crash_witness, root, Path("/reader-witness"), publish, publish_bytes))
            if crash is not None and crash.done():
                crash.result()
            await asyncio.sleep(0.1)
        if case == "recovery":
            assert crash is not None, "Recovery qualification omitted its reader supervisor crash."
            await asyncio.shield(crash)
        minimum_hellos = 1 if case == "recovery" else 2
        if calls["model"] != expected_model_calls or any(calls[name] != expected_jobs for name in ("start", "complete")) or calls["hello"] < minimum_hellos:
            raise AssertionError("Connected initial/restart witness changed the intended number of scientific executions")
        if calls["download"] != (0 if direct_case else 1):
            raise AssertionError("Connected initial/restart witness repeated or omitted Upload acquisition")
        scope = "local signed API; actual Compose and reader-supervisor recovery; no Job, model call or scientific input/inference" if case == "recovery" else "local signed API; scripted model; actual Compose/native path"
        publish("receipt.json", {"calls": calls, "scope": scope})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--delivery-project", required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--case", choices=("pasted", "smiles", "references", "capacity", "successive", "recovery"), required=True)
    options = parser.parse_args()
    asyncio.run(serve(options.root, options.seconds, options.case, options.delivery_project))


if __name__ == "__main__":
    main()
