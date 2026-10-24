"""Disposable signed peers for the final Compose path, never a provider entrypoint.

Reuse ProcessPeers' signed API and RuntimePeers' scripted interpretation, with
SuppliedLifecycle's independent coordinate/unit oracle. Shift checks here cover
population and finiteness, not numerical accuracy; the existing scientific
reference/capacity witnesses remain separate obligations. Scripted interpretation
is not a live model or deployed API witness.
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
from pathlib import Path
from urllib.parse import urlsplit


async def serve(root, store_root, seconds):
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
    from tests.integration.nmrnet.config_ports import ConfigFiles
    from tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle import NM_TABLE, nm_conversion_oracle
    from tests.integration.nmrnet.process_ports import ProcessPeers
    from tests.integration.nmrnet.runtime_ports import RuntimePeers
    from nmrnet_provider.config import ANALYSIS_KIND_REF, load_provider_config
    from provider_core.job_input import verify_job_input

    os.umask(0o077)
    peer = ProcessPeers()
    peer.route, peer.output = "pasted", {"verbosity": "max"}
    peer.ports.source_text = NM_TABLE.decode()
    peer.ports.instructions = verify_job_input(response(NM_TABLE), feed=facts(NM_TABLE))
    calls = {"model": 0, "start": 0, "complete": 0, "hello": 0}
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
        if calls["model"] != 1:
            raise AssertionError("The connected witness must not repeat successful interpretation")
        return await RuntimePeers.model_reply(peer, request, body)

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
        result = await ProcessPeers.reply(peer, request, body)
        if path.endswith("/complete"):
            calls["complete"] += 1
            if calls["complete"] != 1:
                raise AssertionError("The no-outage witness must publish its result once")
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

    async with asyncio.timeout(seconds):
        async with TlsPeer("success", response_factory=model_reply) as model:
            peer.model_peer = model
            async with api_tls_peer("success", response_factory=api_reply) as api:
                peer.peer = api
                source = root / "configuration"
                source.mkdir(mode=0o700)
                for name, owner in (("api-ca.pem", api), ("model-ca.pem", model)):
                    (source / name).write_bytes((Path(owner.scratch.name) / "cert.pem").read_bytes())
                files = ConfigFiles(source, api_origin=api.origin, model_origin=model.origin, store_origin=api.origin,
                                    ca_file=source / "api-ca.pem", model_ca_file=source / "model-ca.pem")
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
                if any(calls[name] != 1 for name in ("model", "start", "complete")) or calls["hello"] < 2:
                    raise AssertionError("Connected initial/restart witness did not preserve one scientific execution")
                publish("receipt.json", {"calls": calls, "scope": "local signed API; scripted model; actual Compose/native path"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    options = parser.parse_args()
    asyncio.run(serve(options.root, options.store_root, options.seconds))


if __name__ == "__main__":
    main()
