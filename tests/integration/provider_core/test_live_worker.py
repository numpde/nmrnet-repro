"""Join installed core, released science and host retirement in separate containers.

Run the live case through make test/integration/provider-scientific-worker.
The loopback TLS peer models only the pinned API wire contract; it is not a
deployed API or an independent scientific accuracy reference.
"""

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import math
import os
import sys
import unittest
from builtins import BaseExceptionGroup
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from http_sf import parse
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, complete_response, observation, progress_response, start_response
from tests.fakes.job_input import facts, response

import provider_core
from nmrnet_provider.retirement import HostRetirement
from nmrnet_provider.session import NMRNetSession
from nmrnet_provider.worker_client import PredictionClient
from provider_core.api import ProviderAPI
from provider_core.attempt_record import MAX_RECORD_BYTES, LocalExecutionPhase, TerminalConfirmed, TerminalPending
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.controller import AttemptController
from provider_core.ownership import AttemptClaims
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport

# A self-authored chloromethane geometry, in angstrom. This is a wiring fixture,
# not an experimental geometry or a claim of independent prediction accuracy.
RAW = json.dumps({"coordinate_unit": "angstrom", "atoms": ["C", "H", "H", "H", "Cl"],
                  "coordinates": [[0, 0, 0], [1.03, 0, -0.36], [-0.51, 0.89, -0.36],
                                  [-0.51, -0.89, -0.36], [0, 0, 1.78]]}).encode()


class RoutedPeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_retention_observation_still_attempts_host_cleanup(self):
        retirement = CheckedRetirement(allocation="fixture", request_directory=Path("/unused/request"),
                                       receipt_directory=Path("/unused/receipt"), operation_seconds=1)
        retirement.confirmed = False
        cancellation = asyncio.CancelledError()
        retirement.store = SimpleNamespace(records=AsyncMock(side_effect=cancellation))
        with patch.object(HostRetirement, "retire", new=AsyncMock()) as host:
            with self.assertRaises(BaseExceptionGroup) as failure:
                await retirement.retire()
            host.assert_awaited_once()
            self.assertIs(failure.exception.exceptions[0], cancellation)

    async def test_reply_can_follow_the_received_operation_instead_of_poll_count(self):
        seen = []

        async def reply(request, body):
            seen.append((request.split(b"\r\n", 1)[0], body))
            return b"wire reply"

        async with api_tls_peer("success", response_factory=reply) as peer:
            import httpx  # noqa: PLC0415 -- Only this transport witness needs a raw request.

            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            result = await transport.exchange(httpx.Request("POST", peer.origin + "/route", content=b"exact"), maximum_bytes=100)
            self.assertEqual(result.body, b"wire reply")
            self.assertEqual(seen, [(b"POST /route HTTP/1.1", b"exact")])
            await transport.retire()


class CheckedClient(PredictionClient):
    """Observe durable entry at the actual worker invocation, without replacing it."""

    def __init__(self, reader, writer, *, allocation, operation_seconds):
        super().__init__(reader, writer, allocation=allocation, operation_seconds=operation_seconds)
        self.preparations = self.executions = 0

    async def prepare(self, requests):
        (current,) = await self.store.records()
        if current.local_phase is not LocalExecutionPhase.EXECUTION_ENTERED:
            raise AssertionError("Scientific preparation preceded durable entry")
        self.prepared_identity = current.prepared_identity
        identity = json.loads(self.prepared_identity.content)
        if [item["request_sha256"] for item in identity["geometries"]] != [hashlib.sha256(raw).hexdigest() for raw in requests]:
            raise AssertionError("Dispatched scientific batch differs from its durable ordered identity")
        self.requests = requests
        self.preparations += 1
        return await super().prepare(requests)

    async def execute(self, handle):
        current, = await self.store.records()
        if current.local_phase is not LocalExecutionPhase.EXECUTION_ENTERED:
            raise AssertionError("Scientific execution preceded durable entry")
        self.executions += 1
        return await super().execute(handle)


class CheckedRetirement(HostRetirement):
    async def retire(self):
        if self.confirmed:
            return
        failures = []
        try:
            self.retained, = await self.store.records()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(cause)
        try:
            await super().retire()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(cause)
        if failures:
            raise BaseExceptionGroup("Qualification observation or host retirement failed", failures)
        if Path("/ipc/science.sock").exists() or Path("/ipc/ready").exists():
            raise AssertionError("Host confirmed retirement before worker grant cleanup")
        self.confirmed = True


@unittest.skipUnless(os.environ.get("NMRNET_LIVE_WORKER") == "1", "run make test/integration/provider-scientific-worker")
class LiveWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_result_is_retained_retired_published_and_reopened(self):
        async with asyncio.timeout(150):
            await self.run_lifecycle()

    async def run_lifecycle(self):
        allocation = os.environ["NMRNET_QUALIFICATION_ALLOCATION"]
        metadata = json.loads(Path("/ipc/metadata.json").read_bytes())
        selection = json.loads(Path("/input/prediction-models.json").read_bytes())
        artifacts = {
            "dictionary": {key: selection["dictionary"][key] for key in ("archive_sha256", "member", "size", "sha256")},
            "elements": {head: {role: {"archive_sha256": selection["checkpoint_archive"]["archive_sha256"], **member}
                                for role, member in pair.items()} for head, pair in selection["elements"].items()},
        }
        self.counts = Counter()
        self.key = Ed25519PrivateKey.generate()
        self.retirement = CheckedRetirement(allocation=allocation, request_directory=Path("/control/request"),
                                            receipt_directory=Path("/control/receipt"), operation_seconds=30)
        self.retirement.confirmed = False
        async with api_tls_peer("success", response_factory=self.reply) as peer:
            self.peer = peer
            recovery_scope = replace(scope(), api_origin=peer.origin, generation_id=allocation)
            pending = replace(record(), scope=recovery_scope, input=facts(RAW))
            transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=1)
            client = ProviderAPI(
                SigningIdentity(peer.origin, "credential:test", self.key), transport, expected_topology="dev-local"
            )
            store_path = Path("/state/attempts")
            store_path.mkdir(mode=0o700)
            self.store = AttemptStore(store_path, scope=recovery_scope,
                                      limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=2)
            await self.store.open()
            self.retirement.store = self.store
            session = None
            controller = None
            try:
                reader, writer = await asyncio.open_unix_connection("/ipc/science.sock")
                self.worker = CheckedClient(reader, writer, allocation=allocation, operation_seconds=90)
                self.worker.store, self.worker.executions = self.store, 0
                session = NMRNetSession(worker=self.worker, retirement=self.retirement,
                                        execution_identity=self.executed_sources(),
                                        selected_artifacts=artifacts, runtime_expectations=metadata["runtime"])
                controller = AttemptController(client, self.store, AttemptClaims(), session,
                                               allowed_job_states=frozenset({"open"}), poll_seconds=0.2, task_settle_seconds=1)
                confirmed = await controller.run(pending)
                terminal = confirmed.pending
                self.assertTrue(self.retirement.confirmed)
                self.assertEqual(terminal, self.retirement.retained)
                self.assertEqual(self.worker.executions, 1)
                self.assertIsInstance(confirmed, TerminalConfirmed)
                self.assertEqual(await self.store.records(), (confirmed,))
            finally:
                # Test instrumentation must not prevent cleanup or replace the
                # original failure. All owners are attempted independently.
                primary = sys.exception()
                failures = []
                retirement = self.retirement.retire if session is None else session.retire
                operations = ((controller.settle,) if controller is not None else ())
                for operation in (*operations, retirement, self.store.close, transport.retire):
                    try:
                        await operation()
                    except (Exception, asyncio.CancelledError) as cause:
                        failures.append(cause)
                if failures:
                    raise BaseExceptionGroup("Composed qualification failed during cleanup",
                                             ([primary] if primary is not None else []) + failures)
            reopened = AttemptStore(store_path, scope=recovery_scope,
                                    limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=2)
            await reopened.open()
            try:
                self.assertEqual(await reopened.records(), (confirmed,))
            finally:
                await reopened.close()
            self.assertEqual(self.counts["start"], 1)
            self.assertEqual(self.counts["input"], 1)
            self.assertEqual(self.counts["complete"], 1)
            self.assertEqual(self.counts["preparing"], 1)
            self.assertEqual(self.counts["running"], 1)
            self.assertGreater(self.counts["read"], 3)
        content = base64.b64decode(json.loads(terminal.body)["canonical_result_base64"], validate=True)
        Path("/state/qualification.json").write_text(json.dumps({
            "schema_id": "nmrnet.provider_lifecycle.qualification.v1",
            "scope": "real released science and host retirement; local API wire peer, not deployed API or accuracy proof",
            "execution": self.execution,
            "controller_image_id": os.environ["NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID"],
            "installed_core_version": importlib.metadata.version("provider-core"),
            "runtime": metadata["runtime"], "selected_artifacts": artifacts,
            "scientific_result": {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)},
            "scientific_executions": self.worker.executions, "api_operations": dict(self.counts),
            "host_retirement_confirmed": self.retirement.confirmed, "terminal_confirmation_reopened": True,
        }, indent=2, sort_keys=True) + "\n")
        Path("/control/request/observed").write_text("retired " + allocation + "\n")

    def executed_sources(self):
        self.execution = qualification_execution_identity(
            (("tests/integration/provider_worker/scientific_probe.py", Path("/source/scientific_probe.py")),)
        )
        return self.execution

    async def reply(self, request, body):
        self.assertLess(sum(self.counts.values()), 600, "Qualification peer request budget exhausted")
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        verify_signed_request(request, body, origin=self.peer.origin, key=self.key)
        if path == "/provider/v1/execution-attempts/start":
            self.assertEqual(method, "POST")
            self.assertEqual(self.counts["start"], 0)
            retained, = await self.store.records()
            self.assertEqual(retained.body, body)
            self.counts["start"] += 1
            return start_response()
        self.assertEqual(self.counts["start"], 1)
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF:
            self.assertEqual(method, "GET")
            self.counts["read"] += 1
            return observation()
        if path == "/provider/v1/jobs/job:example/input":
            self.assertEqual(method, "GET")
            self.counts["input"] += 1
            return response(RAW)
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF + "/progress":
            self.assertEqual(method, "PUT")
            phase = json.loads(body)["phase"]
            self.assertIn(phase, {"preparing", "running"})
            self.counts[phase] += 1
            return progress_response(phase=phase)
        self.assertEqual(path, "/provider/v1/execution-attempts/complete")
        self.assertEqual(method, "POST")
        self.assertTrue(self.retirement.confirmed, "Publication preceded host-confirmed retirement")
        retained, = await self.store.records()
        self.assertIsInstance(retained, TerminalPending)
        self.assertEqual(body, retained.body)
        content = base64.b64decode(json.loads(body)["canonical_result_base64"], validate=True)
        result = json.loads(content)
        geometry, = result["geometries"]
        self.assertEqual(geometry["prepared_request"], json.loads(RAW))
        self.assertEqual([(row["atom_index"], row["element"]) for row in geometry["predictions"]],
                         [(0, "C"), (1, "H"), (2, "H"), (3, "H")])
        self.assertTrue(all(math.isfinite(row["shift_ppm"]) for row in geometry["predictions"]))
        self.assertEqual(set(geometry["loaded_elements"]), {"H", "C"})
        evidence = json.loads(retained.evidence.content)
        (observed_geometry,) = evidence["geometries"]
        self.assertEqual(observed_geometry["request_sha256"], geometry["request_sha256"])
        self.assertEqual(observed_geometry["provenance"]["runtime"], result["runtime"])
        self.assertEqual(observed_geometry["provenance"]["artifacts"], result["selected_models"])
        self.counts["complete"] += 1
        return complete_response(result_schema_id=result["schema_id"], result_byte_length=len(content),
                                 result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest())

def qualification_execution_identity(extra_sources):
    """Bind mounted source bytes separately from image IDs in both live witnesses."""
    from tools.baseline_prepare import execution_identity  # noqa: PLC0415 -- Live lanes grant these observation tools.

    sources = [
        ("tests/integration/provider_core/test_live_worker.py", Path(__file__)),
        ("requirements/provider-core.lock", Path("/core-runtime.lock")),
        ("requirements/provider-core-build.lock", Path("/core-build.lock")),
        ("packages/provider-core/pyproject.toml", Path("/package/pyproject.toml")),
        *extra_sources,
    ]
    for name in ("nmrnet_provider", "nmrnet_inference", "tools"):
        sources.extend((str(path.relative_to("/consumer")), path) for path in (Path("/consumer") / name).glob("*.py"))
    core_root = Path(provider_core.__file__).parent
    sources.extend(("packages/provider-core/src/provider_core/" + path.name, path) for path in core_root.glob("*.py"))
    tests_root = Path(__file__).parents[2]
    sources.extend(("packages/provider-core/tests/fakes/" + path.name, path) for path in (tests_root / "fakes").glob("*.py"))
    sources.append(("packages/provider-core/tests/run_suite.py", tests_root / "run_suite.py"))
    # Unittest executes these package initializers before the selected test.
    sources.extend(
        (
            ("packages/provider-core/tests/__init__.py", tests_root / "__init__.py"),
            ("packages/provider-core/tests/integration/__init__.py", tests_root / "integration/__init__.py"),
            ("tests/integration/provider_core/__init__.py", Path(__file__).parent / "__init__.py"),
        )
    )
    sources.extend(
        ("tools/" + name, Path("/consumer/tools") / name)
        for name in (
            "worker_qualification.sh",
            "worker_retirement.sh",
            "docker_client.sh",
            "inference_native.sh",
            "git_metadata.sh",
        )
    )
    return execution_identity(
        image_id=os.environ["NMRNET_QUALIFICATION_IMAGE_ID"],
        repository_revision=os.environ["NMRNET_QUALIFICATION_REPOSITORY_REVISION"],
        tool_sources=sources,
    )


def verify_signed_request(request, body, *, origin, key):
    """Verify the local peer's exact request, digest and signing-key binding."""
    method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
    headers = {
        name.lower(): value.strip()
        for line in request.split(b"\r\n")[1:]
        if b":" in line
        for name, value in [line.split(b":", 1)]
    }
    parsed = urlsplit(target)
    lines = [
        b'"@method": ' + method.encode(),
        b'"@authority": ' + origin.removeprefix("https://").encode(),
        b'"@path": ' + parsed.path.encode(),
        b'"@query": ?' + parsed.query.encode(),
    ]
    # None identifies the API's bodyless read-capability POST. Other mutation
    # routes carry JSON, including an empty body if explicitly supplied.
    if method in {"POST", "PUT"} and body is not None:
        if parse(headers[b"content-digest"], tltype="dictionary")["sha-256"][0] != hashlib.sha256(body).digest():
            raise AssertionError("Signed request digest differs from its exact body")
        lines += [b'"content-type": application/json', b'"content-digest": ' + headers[b"content-digest"]]
    elif body not in (None, b"") or b"content-type" in headers or b"content-digest" in headers:
        raise AssertionError("Bodyless signed request unexpectedly carries body content or its headers")
    lines.append(b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1])
    signature = parse(headers[b"signature"], tltype="dictionary")["sig1"][0]
    key.public_key().verify(signature, b"\n".join(lines))
