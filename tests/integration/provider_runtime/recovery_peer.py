"""Serve one synthetic terminal replay, then empty discovery for operator recovery.

This fixture reuses RecoveryPeers.retain and ProcessPeers.reply. It creates no
scientific worker and supplies no Jobs. The host owns exact container retirement;
the final receipt proves only the fixture assertions and joined TLS handlers.
"""

from tools.container_guard import require_container

require_container()

import argparse
import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from tests.fakes.api_tls import api_tls_peer
from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.process_ports import ProcessPeers
from tests.integration.nmrnet.recovery_ports import RecoveryPeers

from nmrnet_provider.config import load_recovery_config
from nmrnet_provider.process import _publish
from provider_core.attempt_record import TerminalConfirmed


def publish(root, name, document):
    _publish(root, name, (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))


async def serve(root, seconds):
    os.umask(0o077)
    metadata = root.lstat()
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("Recovery peer requires a canonical caller-owned directory with mode 0700.")
    source, state = root / "source", root / "state"
    source.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    retained = RecoveryPeers()
    idle = None
    completions = 0
    idle_request = asyncio.Event()

    async def reply(request, body):
        nonlocal completions
        if idle is None:
            # The existing recovery fixture verifies the exact signing identity.
            response = await retained.reply(request, body)
            if request.split(b"\r\n", 1)[0] != b"POST /provider/v1/execution-attempts/complete HTTP/1.1":
                raise AssertionError("Stopped recovery permits only its retained COMPLETE request.")
            if body != retained.record.body or completions != 0:
                raise AssertionError("Stopped recovery must publish the exact terminal body once.")
            completions += 1
            return response
        response = await ProcessPeers.reply(idle, request, body)
        if idle.discovered.is_set():
            idle_request.set()
        return response

    async with asyncio.timeout(seconds):
        async with api_tls_peer("success", response_factory=reply) as peer:
            retained.peer = peer
            ca = source / "api-ca.pem"
            _publish(source, ca.name, (Path(peer.scratch.name) / "cert.pem").read_bytes())
            retained.files = ConfigFiles(
                source, api_origin=peer.origin, model_origin=peer.origin, store_origin=peer.origin, ca_file=ca, maximum_records=3
            )
            old_state = retained.files.state
            retained.files.state = state
            retained.files.raw = retained.files.raw.replace(str(old_state).encode(), str(state).encode())
            retained.files.write(retained.files.raw)
            retained.config = load_recovery_config(retained.files.path)
            selector = await retained.retain("complete")
            original = retained.record
            hidden = {path: path.read_bytes() for path in (retained.files.credential, ca, retained.files.key)}
            for path in hidden:
                path.unlink()
            authority = urlsplit(peer.origin).netloc
            ready = {
                "config": str(retained.files.path),
                "source_root": str(source),
                "store_root": str(state),
                "record_sha256": selector,
                "authority": authority,
                "terminal_body_sha256": hashlib.sha256(original.body).hexdigest(),
                "protected_evidence_sha256": hashlib.sha256(original.evidence.content).hexdigest(),
            }
            for key, name in (
                ("config", "config.txt"),
                ("source_root", "source-root.txt"),
                ("store_root", "store-root.txt"),
                ("record_sha256", "record-sha256.txt"),
                ("authority", "authority.txt"),
            ):
                _publish(root, name, (ready[key] + "\n").encode("ascii"))
            publish(root, "ready.json", ready)
            restored = recovered = idled = False
            while not (root / "done").exists():
                if (root / "restore-inputs").exists() and not restored:
                    for path, raw in hidden.items():
                        _publish(path.parent, path.name, raw)
                    restored = True
                    publish(root, "inputs-restored", {"restored": True})
                if (root / "idle-phase").exists() and not recovered:
                    if not restored or completions != 1:
                        raise AssertionError("Ordinary restart follows exactly one accepted terminal replay.")
                    records = await retained.records()
                    if (
                        len(records) != 1
                        or type(records[0]) is not TerminalConfirmed
                        or records[0].pending != original
                        or records[0].receipt.replayed is not True
                    ):
                        raise AssertionError("Recovery changed the original terminal command or protected evidence.")
                    idle = SimpleNamespace(
                        peer=peer,
                        config=retained.config,
                        events=[],
                        bad_reply=False,
                        model_peer=None,
                        offer_job=False,
                        discovered=asyncio.Event(),
                    )
                    recovered = True
                    publish(
                        root,
                        "recovered",
                        {
                            "phase": "TerminalConfirmed",
                            "terminal_body_sha256": hashlib.sha256(original.body).hexdigest(),
                            "protected_evidence_sha256": hashlib.sha256(original.evidence.content).hexdigest(),
                        },
                    )
                if idle_request.is_set() and not idled:
                    idled = True
                    publish(root, "idled", {"empty_jobs_request_received": True})
                await asyncio.sleep(0.02)
            if not recovered or not idled or completions != 1:
                raise AssertionError("The fixture did not receive its one replay followed by ordinary empty discovery.")
            if "hello" not in idle.events or "execution-attempts" not in idle.events or "jobs" not in idle.events:
                raise AssertionError("Ordinary restart did not send Hello, provider inventory and empty discovery.")
        # TlsPeer.__aexit__ joins every handler and propagates callback failures.
        records = await retained.records()
        if (
            len(records) != 1
            or type(records[0]) is not TerminalConfirmed
            or records[0].pending != original
            or records[0].receipt.replayed is not True
        ):
            raise AssertionError("Ordinary idle restart changed the original completed record or private evidence.")
        publish(
            root,
            "summary.json",
            {
                "complete_requests": completions,
                "terminal_body_sha256": hashlib.sha256(original.body).hexdigest(),
                "protected_evidence_sha256": hashlib.sha256(original.evidence.content).hexdigest(),
                "idle_routes": idle.events,
                "jobs_supplied": 0,
                "scientific_work_performed": False,
                "tls_handlers_settled": True,
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    arguments = parser.parse_args()
    if not 1 <= arguments.seconds <= 180:
        parser.error("the fixture requires a finite budget from 1 through 180 seconds")
    asyncio.run(serve(arguments.root, arguments.seconds))
