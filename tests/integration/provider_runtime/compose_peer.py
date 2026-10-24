"""Disposable signed peers for the final Compose path, never a provider entrypoint.

Reuse ProcessPeers' signed API and RuntimePeers' scripted interpretation, with
SuppliedLifecycle's independent coordinate/unit oracle. Shift checks here cover
population and finiteness for the pasted case. Reference and capacity cases reuse
their existing independent owners. Scripted interpretation is not a live model
or deployed API witness.
"""

from tools.container_guard import require_container

require_container()

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import sys
from decimal import Decimal
from fractions import Fraction
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import urlsplit


def require_exported_result(result, case, manifest):
    from tests.integration.nmrnet.reference_fixtures import PRODUCTION_SHA256, REFERENCE_SHA256, pinned_packet
    from tests.integration.nmrnet.reference_service import require_reference_predictions

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


async def serve(root, store_root, seconds, case):
    # Match provider_core_tests' installed-consumer fixture layout. These are
    # explicit read-only qualification inputs; production images import no tests.
    fixture = Path("/tmp/compose-peer")
    shutil.copytree("/fixture/core-tests", fixture / "tests")
    shutil.copytree("/fixture/binding-tests", fixture / "tests/integration/nmrnet")
    sys.path.insert(0, str(fixture))
    from tests.fakes.api_tls import api_tls_peer
    from tests.fakes.job_feed import page
    from tests.fakes.job_input import facts, response
    from tests.fakes.tls_peer import TlsPeer
    from tests.fakes.acquisition import store_headers
    from tests.fakes.uploads import UPLOAD_REF
    from tests.integration.nmrnet.config_ports import ConfigFiles
    from tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle import NM_TABLE, nm_conversion_oracle
    from tests.integration.nmrnet.process_ports import ProcessPeers
    from tests.integration.nmrnet.runtime_ports import RuntimePeers, require_download_credentials, require_model_credentials
    from tests.integration.nmrnet.interpreter_ports import tool_reply
    from tests.integration.nmrnet.scientific_inputs import load_scientific_inputs
    from nmrnet_provider.config import ANALYSIS_KIND_REF, load_provider_config
    from provider_core.job_input import verify_job_input

    os.umask(0o077)
    peer = ProcessPeers()
    peer.output = {"verbosity": "max"}
    if case == "references":
        # Seventeen digits preserve a binary64 scalar's round trip. Compare the
        # original unrounded reference values, without changing product defaults
        # or weakening the existing exact population comparison.
        peer.output["shift_significant_digits"] = 17
    if case == "pasted":
        peer.route = "pasted"
        peer.ports.source_text = NM_TABLE.decode()
        instructions = NM_TABLE
        expected_model_calls = 1
        maximum_records = 1
    else:
        peer.route = "upload"
        peer.ports.source, manifest = load_scientific_inputs(Path("/fixture/scientific_inputs"), case)
        maximum_records = len(manifest["records"])
        instructions = f"Predict Upload {UPLOAD_REF}, all {maximum_records} records, in angstroms.".encode()
        expected_model_calls = 2
    peer.ports.instructions = verify_job_input(response(instructions), feed=facts(instructions))
    calls = {"model": 0, "start": 0, "complete": 0, "hello": 0, "download": 0}
    oracle_bytes = Path("/fixture/expected.json").read_bytes()
    expected = json.loads(oracle_bytes, parse_float=Decimal)["geometries"]["chloromethane"]

    def publish(name, value):
        content = (json.dumps(value, sort_keys=True) + "\n").encode()
        pending = root / ("." + name + ".pending")
        with pending.open("xb") as stream:
            stream.write(content)
        pending.replace(root / name)

    async def model_reply(request, body):
        calls["model"] += 1
        if calls["model"] > expected_model_calls:
            raise AssertionError("The connected witness must not repeat successful interpretation")
        if case != "pasted":
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
        path = urlsplit(request.split(b"\r\n", 1)[0].decode().split()[1]).path
        if path.endswith("/hello"):
            calls["hello"] += 1
        if path.endswith("/start"):
            calls["start"] += 1
            if calls["start"] != 1:
                raise AssertionError("The connected witness must not create another Attempt")
        if path == "/provider/v1/jobs" and peer.result is not None:
            # Keep discovery signed even after withdrawing the completed fixture.
            from tests.integration.nmrnet.test_live_worker import verify_signed_request

            verify_signed_request(request, body, origin=peer.peer.origin, key=peer.config.api.identity.private_key)
            empty = page()
            empty.update(analysis_kind_ref=ANALYSIS_KIND_REF, jobs=[], next_cursor=None)
            return json.dumps(empty).encode()
        if path.endswith("/read-capability"):
            from tests.integration.nmrnet.test_live_worker import verify_signed_request

            verify_signed_request(request, None, origin=peer.peer.origin, key=peer.config.api.identity.private_key)
            result = await RuntimePeers.api_reply(peer, request, body)
        else:
            result = await ProcessPeers.reply(peer, request, body)
        if path.endswith("/complete"):
            calls["complete"] += 1
            if calls["complete"] != 1:
                raise AssertionError("The no-outage witness must publish its result once")
            if case != "pasted":
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
            publish("completed.json", {"calls": calls, "coordinate_oracle_sha256": hashlib.sha256(oracle_bytes).hexdigest(),
                                       "result_sha256": hashlib.sha256((root / "result.json").read_bytes()).hexdigest()})
        return result

    async with asyncio.timeout(seconds), AsyncExitStack() as peers:
        model = await peers.enter_async_context(TlsPeer("success", response_factory=model_reply))
        peer.model_peer = model
        api = await peers.enter_async_context(api_tls_peer("success", response_factory=api_reply))
        if case == "pasted":
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
                            maximum_records=maximum_records, ca_file=source / "api-ca.pem",
                            model_ca_file=source / "model-ca.pem", store_ca_file=source / "store-ca.pem")
        peer.config = load_provider_config(files.path)
        raw = files.raw.replace(str(files.state).encode(), b"/srv/nmrnet/attempts")
        raw = raw.replace(str(source).encode(), b"/srv/nmrnet/input")
        (root / "provider.toml").write_bytes(raw)
        routes = "\n".join(f'[routes.{role}]\nmode = "mapped"\ninterface = "lo"\naddresses = ["127.0.0.1=127.0.0.1"]\n'
                           for role in ("api", "interpreter", "store"))
        (root / "deployment.toml").write_text(
            f'store_root = {json.dumps(str(store_root))}\nmaximum_startup_bytes = 4194304\n' + routes)
        publish("ready.json", {"api_origin": api.origin, "model_origin": model.origin})
        while not (root / "done").exists():
            await asyncio.sleep(0.1)
        if calls["model"] != expected_model_calls or any(calls[name] != 1 for name in ("start", "complete")) or calls["hello"] < 2:
            raise AssertionError("Connected initial/restart witness did not preserve one scientific execution")
        if calls["download"] != (0 if case == "pasted" else 1):
            raise AssertionError("Connected initial/restart witness repeated or omitted Upload acquisition")
        publish("receipt.json", {"calls": calls, "scope": "local signed API; scripted model; actual Compose/native path"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--case", choices=("pasted", "references", "capacity"), required=True)
    options = parser.parse_args()
    asyncio.run(serve(options.root, options.store_root, options.seconds, options.case))


if __name__ == "__main__":
    main()
