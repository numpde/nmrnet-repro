"""One native lifecycle shared by three concrete molecular input fixtures.

Extracted from the qualified SMILES proof at 6f8a763. It keeps the existing
configured/core owners, signed local peers, exact replay and host cleanup.
Route-local classes supply source operations and independent molecular oracles.
"""

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import sys
import tempfile
from builtins import BaseExceptionGroup
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from tests.fakes.acquisition import store_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import ATTEMPT_REF, complete_response, observation, progress_response, start_response
from tests.fakes.job_feed import page as feed_page
from tests.fakes.job_input import facts, response
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.test_live_worker import (
    CheckedClient,
    CheckedRetirement,
    qualification_execution_identity,
    verify_signed_request,
)

from nmrnet_provider.config import ANALYSIS_KIND_REF, load_provider_config
from nmrnet_provider.retirement import HostRetirement, retire_worker_channel
from nmrnet_provider.runtime import assemble_attempt
from provider_core.api import ProviderAPI
from provider_core.attempt_record import StartPending, TerminalConfirmed, TerminalPending
from provider_core.attempt_request import start_body
from provider_core.attempt_store import AttemptStore
from provider_core.controller import ControllerFailed
from provider_core.execution import ObservationRecovery
from provider_core.ownership import AttemptClaims
from provider_core.publication import PublicationFailed
from provider_core.recovery import AttemptRecovery


def require_host_source_closure(execution, manifest_bytes):
    """Join observed mounted-code hashes to the host's independently captured list."""
    manifest = {name: digest for digest, name in (line.split("  ", 1) for line in manifest_bytes.decode().splitlines())}
    for name, observed in execution["tool_sources"].items():
        if name not in manifest or observed["sha256"] != manifest[name]:
            raise AssertionError(
                f"Cannot qualify mounted source {name}: its hash is absent from, or differs from, the host's source manifest."
            )


async def settle_fixture(operations, *, primary):
    """Attempt each owned cleanup without erasing the original failure/cancellation."""
    failures = []
    for operation in operations:
        try:
            await operation()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(cause)
    raise_fixture_failures(failures, primary=primary)


def raise_fixture_failures(failures, *, primary=None):
    """Keep independent cleanup evidence and the original interruption identity."""
    if not failures:
        return
    causes = ([primary] if primary is not None else []) + list(failures)
    combined = BaseExceptionGroup("Native lifecycle qualification cleanup failed", causes)
    for cause in causes:
        if isinstance(cause, asyncio.CancelledError):
            combined.interruption_cause = cause.__cause__
            raise cause from combined
    raise combined


class PrestartedSourcesRetirement(CheckedRetirement):
    """Own source hosts started by this proof before the normal factory runs.

    The host services source retirement before science retirement. Observe unused
    prestarted grants here as well, so an early model refusal cannot deadlock that
    order. This extra fixture authority never belongs to production preparation.
    """

    def __init__(self, *, source_hosts, **configuration):
        super().__init__(**configuration)
        self.source_hosts = source_hosts

    async def retire(self):
        operations = [host.retire for host in self.source_hosts]
        operations.append(super().retire)
        await settle_fixture(operations, primary=None)


class GeometryRetirement(HostRetirement):
    confirmed = False

    async def retire(self):
        await super().retire()
        if Path("/geometry-ipc/geometry.sock").exists() or Path("/geometry-ipc/ready").exists():
            raise AssertionError("Geometry-worker retirement preceded removal of its private IPC grants")
        self.confirmed = True


class PreparedPredictionClient(CheckedClient):
    async def prepare(self, requests):
        if not self.geometry_host.confirmed:
            raise AssertionError("Scientific dispatch preceded confirmed geometry-worker retirement")
        return await super().prepare(requests)


class NativeLifecycle:
    """One configured lifecycle spine; route tests supply molecular selection and oracles."""

    maximum_records = 1
    state_root = Path("/state")
    expected_preparation_calls = 1

    @asynccontextmanager
    async def configured_peers(self):
        async with AsyncExitStack() as stack:
            peer = await stack.enter_async_context(api_tls_peer("success", response_factory=self.reply))
            model = await stack.enter_async_context(TlsPeer("success", response_factory=self.model_reply))
            store_options = {}
            if self.input_kind == "upload":
                headers = tuple((name, value) for name, value in store_headers(self.source) if name != b"content-length")
                self.store_peer = await stack.enter_async_context(
                    TlsPeer("success", response_body=self.source, response_headers=headers)
                )
                store_options = {
                    "store_origin": self.store_peer.origin,
                    "store_ca_file": Path(self.store_peer.scratch.name) / "cert.pem",
                }
            with tempfile.TemporaryDirectory() as directory:
                files = ConfigFiles(
                    directory,
                    maximum_records=self.maximum_records,
                    api_origin=peer.origin,
                    model_origin=model.origin,
                    ca_file=Path(peer.scratch.name) / "cert.pem",
                    model_ca_file=Path(model.scratch.name) / "cert.pem",
                    **store_options,
                )
                raw = files.raw.replace(b"seed = 17", f"seed = {self.generation_settings.seed}".encode())
                raw = raw.replace(b"max_iterations = 200", f"max_iterations = {self.generation_settings.max_iterations}".encode())
                raw = raw.replace(json.dumps(str(files.state)).encode(), json.dumps(str(self.state_root / "attempts")).encode())
                raw = raw.replace(b"max_worker_calls = 2", f"max_worker_calls = {self.expected_preparation_calls}".encode())
                files.write(raw)
                self.config = load_provider_config(files.path)
                self.key = self.config.api.identity.private_key
                self.model_peer = model
                yield peer

    async def run_lifecycle(self):
        self.counts, self.model_calls = Counter(), 0
        self.allocation = os.environ["NMRNET_QUALIFICATION_ALLOCATION"]
        self.geometry_host = GeometryRetirement(
            allocation=os.environ["NMRNET_QUALIFICATION_GEOMETRY_ALLOCATION"],
            request_directory=Path("/control/geometry-request"),
            receipt_directory=Path("/control/geometry-receipt"),
            operation_seconds=30,
        )
        self.retirement = PrestartedSourcesRetirement(
            source_hosts=(self.geometry_host,),
            allocation=self.allocation,
            request_directory=Path("/control/request"),
            receipt_directory=Path("/control/receipt"),
            operation_seconds=30,
        )
        self.retirement.confirmed = False
        try:
            await self.joined_lifecycle()
        finally:
            # This guard includes fixture/TLS/store setup, before either live
            # session can adopt the prestarted endpoints. Base host observation
            # remains valid even if no store existed for the checked witness.
            async def retire_science_host():
                await HostRetirement.retire(self.retirement)

            await settle_fixture((self.geometry_host.retire, retire_science_host), primary=sys.exception())

    async def joined_lifecycle(self):
        self.load_case()
        metadata = json.loads(Path("/ipc/metadata.json").read_bytes())
        self.scientific_runtime = metadata["runtime"]
        selection = json.loads(Path("/input/prediction-models.json").read_bytes())
        self.artifacts = {
            "dictionary": {key: selection["dictionary"][key] for key in ("archive_sha256", "member", "size", "sha256")},
            "elements": {
                head: {
                    role: {"archive_sha256": selection["checkpoint_archive"]["archive_sha256"], **member}
                    for role, member in pair.items()
                }
                for head, pair in selection["elements"].items()
            },
        }
        async with self.configured_peers() as peer:
            self.peer = peer
            recovery_scope = self.config.lifecycle.scope
            transport = self.config.api.http.new_transport()
            controller = None
            self.store = self.worker = None
            try:
                client = ProviderAPI(self.config.api.identity, transport, expected_topology=self.config.api.expected_topology)
                discovered = await client.jobs(
                    analysis_kind_ref=ANALYSIS_KIND_REF, has_provider_execution_attempt=False, limit=1, cursor=None
                )
                (selected,) = discovered.jobs
                pending = StartPending(
                    recovery_scope,
                    selected.analysis_kind_ref,
                    selected.input,
                    start_body(selected.input.job_ref, "configured-native:one"),
                )
                store_path = self.config.lifecycle.store_root
                store_path.mkdir(mode=0o700)
                self.store = self.open_store(store_path, recovery_scope)
                await self.store.open()
                self.retirement.store = self.store
                reader, writer = await asyncio.open_unix_connection("/ipc/science.sock")
                self.worker = PreparedPredictionClient(reader, writer, allocation=self.allocation, operation_seconds=90)
                self.worker.store = self.store
                self.worker.geometry_host = self.geometry_host
                root = Path(__file__).parent
                self.execution = qualification_execution_identity(
                    (
                        ("tests/integration/provider_core/native_lifecycle.py", Path(__file__)),
                        (
                            "tests/integration/provider_core/normative/test_nmrnet_smiles_lifecycle.py",
                            root / "normative/test_nmrnet_smiles_lifecycle.py",
                        ),
                        (
                            "tests/integration/provider_core/normative/test_nmrnet_supplied_lifecycle.py",
                            root / "normative/test_nmrnet_supplied_lifecycle.py",
                        ),
                        (
                            "tests/integration/provider_core/adversarial/test_nmrnet_smiles_lifecycle.py",
                            root / "adversarial/test_nmrnet_smiles_lifecycle.py",
                        ),
                        ("tests/integration/provider_core/normative/__init__.py", root / "normative/__init__.py"),
                        ("tests/integration/provider_core/adversarial/__init__.py", root / "adversarial/__init__.py"),
                        ("tests/integration/provider_worker/retirement_probe.py", Path("/workspace/probe.py")),
                        ("tests/integration/provider_worker/scientific_probe.py", Path("/source/scientific_probe.py")),
                        ("requirements/preparation.lock", Path("/input/preparation.lock")),
                        *self.execution_sources(),
                        ("tests/integration/provider_core/config_ports.py", root / "config_ports.py"),
                        ("tools/geometry_image.sh", Path("/consumer/tools/geometry_image.sh")),
                    )
                )
                self.execution.update(
                    geometry_image_id=os.environ["NMRNET_QUALIFICATION_GEOMETRY_IMAGE_ID"],
                    controller_image_id=os.environ["NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID"],
                    startup_configuration_sha256=self.config.source_sha256,
                )
                require_host_source_closure(self.execution, Path("/input/sources.sha256").read_bytes())
                controller = await assemble_attempt(
                    config=self.config,
                    pending=pending,
                    api=client,
                    store=self.store,
                    setup_owner=None,
                    claims=AttemptClaims(),
                    worker=self.worker,
                    retirement=self.retirement,
                    execution_identity=self.execution,
                    selected_artifacts=self.artifacts,
                    runtime_expectations=metadata["runtime"],
                    allocate_reader=self.allocate_reader,
                    allocate_generator=self.allocate_generator,
                    observation_recovery=ObservationRecovery(
                        self.config.lifecycle.observation_gap_seconds,
                        self.config.service.policy.api_backoff_max_seconds,
                        lambda _attempt, _failure, _seconds: self.fail("No observation retry expected in this science witness"),
                        lambda _attempt, _failure: self.fail("No observation recovery expected in this science witness"),
                    ),
                )
                with self.assertRaises(ControllerFailed) as lost:
                    await controller.run(pending)
                self.assertIsInstance(lost.exception.cause, PublicationFailed)
                self.assertTrue(lost.exception.cause.api_failure.attempt_mutation_may_have_committed)
                (terminal,) = await self.store.records()
                self.assertIs(type(terminal), TerminalPending)
                self.assertEqual(terminal, self.retirement.retained)
                self.assertTrue(self.geometry_host.confirmed and self.retirement.confirmed)
                self.assertEqual(self.worker.preparations, 1)
                self.assertEqual(self.worker.executions, 1)
            finally:
                primary = sys.exception()
                operations = []
                if controller is not None:
                    operations.append(controller.settle)
                else:
                    operations.append(self.settle_preflight)
                    if self.worker is not None:
                        operations.append(self.retire_unadopted_science)
                if self.store is not None:
                    operations.append(self.store.close)
                operations.append(transport.retire)
                await settle_fixture(operations, primary=primary)
            confirmed = await self.replay_terminal(store_path, recovery_scope, terminal)
            self.assertEqual(self.model_calls, self.expected_model_calls)
            self.assertEqual(self.geometry_client.calls, 1)
            self.assertEqual((self.worker.preparations, self.worker.executions), (1, 1))
            for name in ("jobs", "start", "input", "preparing", "running"):
                self.assertEqual(self.counts[name], 1)
            self.assertEqual(self.counts["complete"], 2)
            self.assertNotIn("rdkit", sys.modules)
            self.assertNotIn("torch", sys.modules)
        content = base64.b64decode(json.loads(confirmed.pending.body)["canonical_result_base64"], validate=True)
        # This successful finite fixture has exact executed coordinates. Keep
        # its exact published result for review, rather than disposable journals.
        Path("/state/scientific-result.json").write_bytes(content)
        Path("/state/qualification.json").write_text(
            json.dumps(
                {
                    "schema_id": "nmrnet.interpreted_lifecycle.qualification.v1",
                    "scope": "configured assembly and isolated molecular input/released H/C through local signed TLS/store and scripted HTTPS model replies; not external model, deployed API or accuracy",
                    "input_kind": self.input_kind,
                    "fixture_sha256": self.fixture_sha256,
                    "input_fingerprint": facts(self.instructions).input_fingerprint,
                    "geometry_image_id": os.environ["NMRNET_QUALIFICATION_GEOMETRY_IMAGE_ID"],
                    "scientific_image_id": os.environ["NMRNET_QUALIFICATION_IMAGE_ID"],
                    "controller_image_id": os.environ["NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID"],
                    "installed_core_version": importlib.metadata.version("provider-core"),
                    "runtime": self.scientific_runtime,
                    "selected_artifacts": self.artifacts,
                    "scientific_result": {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)},
                    **self.qualification_details(content),
                    "model_exchanges": self.model_calls,
                    "interpretation": json.loads(content)["interpretation"],
                    "scientific_preparations": self.worker.preparations,
                    "scientific_executions": self.worker.executions,
                    "api_operations": dict(self.counts),
                    "host_retirement_confirmed": {
                        "geometry": self.geometry_host.confirmed,
                        "scientific": self.retirement.confirmed,
                    },
                    "terminal_replayed_after_reopen": True,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        Path("/control/request/observed").write_text("retired " + self.allocation + "\n")

    def open_store(self, path, recovery_scope):
        settings = self.config.lifecycle
        return AttemptStore(path, scope=recovery_scope, limits=settings.store_limits, operation_seconds=settings.store_seconds)

    async def settle_preflight(self):
        if self.store is not None and self.store.pending_operation is not None and self.store.pending_owner is None:
            await self.store.settle()

    async def retire_unadopted_science(self):
        failures = await retire_worker_channel(self.worker, self.retirement)
        raise_fixture_failures([cause for _operation, cause in failures])

    async def replay_terminal(self, path, recovery_scope, terminal):
        self.store = self.open_store(path, recovery_scope)
        transport = self.config.api.http.new_transport()
        claim = recovery = None

        async def settle_recovery():
            if claim is not None:
                if claim.operation_owner is not None:
                    await recovery.settle()
                claim.release()

        try:
            await self.store.open()
            client = ProviderAPI(self.config.api.identity, transport, expected_topology=self.config.api.expected_topology)
            self.assertEqual(await self.store.records(), (terminal,))
            (claim,) = AttemptClaims().claim_records((terminal,))
            recovery = AttemptRecovery(client, self.store, claim)
            confirmed = await recovery.recover(terminal)
            self.assertIs(type(confirmed), TerminalConfirmed)
            self.assertTrue(confirmed.receipt.replayed)
            self.assertEqual(confirmed.pending, terminal)
            self.assertEqual(await self.store.records(), (confirmed,))
        finally:
            await settle_fixture(
                (settle_recovery, self.settle_preflight, self.store.close, transport.retire), primary=sys.exception()
            )
        self.store = self.open_store(path, recovery_scope)
        try:
            await self.store.open()
            self.assertEqual(await self.store.records(), (confirmed,))
        finally:
            await settle_fixture((self.settle_preflight, self.store.close), primary=sys.exception())
        return confirmed

    async def reply(self, request, body):
        self.assertLess(sum(self.counts.values()), 600, "Qualification request budget exhausted")
        self.assertNotIn(b"bearer ", request.lower())
        self.assertNotIn(b"private_model_key", request.lower())
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        if path.endswith("/read-capability"):
            self.assertEqual(body, b"")
            verify_signed_request(request, None, origin=self.peer.origin, key=self.key)
        else:
            verify_signed_request(request, body, origin=self.peer.origin, key=self.key)
        if path.endswith(("/uploads", "/read-capability")):
            return await self.upload_reply(method, path, body)
        if path == "/provider/v1/jobs":
            self.assertEqual(method, "GET")
            self.counts["jobs"] += 1
            document = feed_page()
            document.update(analysis_kind_ref=ANALYSIS_KIND_REF, next_cursor=None)
            document["jobs"] = [
                {
                    **asdict(facts(self.instructions)),
                    "analysis_kind_ref": ANALYSIS_KIND_REF,
                    "created_at": "2026-10-18T00:00:00.123456Z",
                }
            ]
            return json.dumps(document).encode()
        if path == "/provider/v1/execution-attempts/start":
            self.assertEqual(method, "POST")
            (retained,) = await self.store.records()
            self.assertEqual(retained.body, body)
            self.counts["start"] += 1
            return start_response(analysis_kind_ref=ANALYSIS_KIND_REF)
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF:
            self.assertEqual(method, "GET")
            self.counts["read"] += 1
            return observation()
        if path == "/provider/v1/jobs/job:example/input":
            self.assertEqual(method, "GET")
            self.counts["input"] += 1
            return response(self.instructions)
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF + "/progress":
            self.assertEqual(method, "PUT")
            phase = json.loads(body)["phase"]
            self.assertIn(phase, {"preparing", "running"})
            self.counts[phase] += 1
            return progress_response(phase=phase)
        self.assertEqual(path, "/provider/v1/execution-attempts/complete")
        self.assertEqual(method, "POST")
        self.assertTrue(self.geometry_host.confirmed and self.retirement.confirmed)
        (retained,) = await self.store.records()
        self.assertIs(type(retained), TerminalPending)
        self.assertEqual(body, retained.body)
        self.assert_result(retained)
        self.counts["complete"] += 1
        if self.counts["complete"] == 1:
            self.first_completion = body
            return None
        self.assertEqual(body, self.first_completion)
        content = base64.b64decode(json.loads(body)["canonical_result_base64"], validate=True)
        return complete_response(
            result_schema_id="nmrnet.coordinate_shifts.result.v1",
            result_byte_length=len(content),
            result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
            replayed=True,
        )
