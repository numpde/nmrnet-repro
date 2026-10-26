"""One scripted interpretation for the real-API witness; no real model claim.

Run only in the controller image with hash-pinned shared test support,
the fixed case read-only, and a separate private /output.
The peer never receives API credentials or the provider's captured configuration.
The case copies SuppliedLifecycle's authored nm table at d622f27, so the earlier
retained scientific receipt remains an independent repeatability comparison.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import hashlib
import json
import os
import signal
from pathlib import Path

from tests.fakes.conversation import call
from tests.fakes.tls_peer import TlsPeer


async def serve():
    os.umask(0o077)
    case = json.loads(Path("/fixture/case.json").read_bytes())
    output = Path("/output")
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
    requests = []

    async def reply(headers, body):
        if requests:
            raise AssertionError("The first API witness permits one model exchange only")
        if b"authorization: bearer private_model_key\r\n" not in headers.lower() or b"signature:" in headers.lower():
            raise AssertionError("The model peer requires only its synthetic model authorization")
        request = json.loads(body)
        if request["model"] != "fixture-model" or request["messages"][1]["content"] != case["specification"]:
            raise AssertionError("The model request differs from the fixed supplied-geometry commission")
        requests.append({"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)})
        (output / "requests.json").write_text(json.dumps(requests) + "\n")
        message = call("pasted", "prepare_pasted", case["arguments"])
        return json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls"}]}).encode()

    async with TlsPeer("success", response_factory=reply, bind_host="0.0.0.0") as peer:
        (output / "ca.pem").write_bytes((Path(peer.scratch.name) / "cert.pem").read_bytes())
        port = peer.server.sockets[0].getsockname()[1]
        ready = output / "ready.pending"
        ready.write_text(json.dumps({"origin": f"https://localhost:{port}", "scripted": True}) + "\n")
        ready.rename(output / "ready.json")
        await stopped.wait()
    if len(requests) != 1:
        raise AssertionError("The completed API witness must make exactly one model request")
    (output / "retired.json").write_text(json.dumps({"requests": len(requests), "handlers_settled": True}) + "\n")


if __name__ == "__main__":
    asyncio.run(serve())
