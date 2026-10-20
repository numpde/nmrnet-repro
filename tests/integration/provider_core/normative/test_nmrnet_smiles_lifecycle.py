"""Native CCCC/N=3 generation through released science and local signed publication.

The chemistry oracle is smiles.json normative.flexible_multiple: atom inventory
and literal H-parent pairs, without coordinate/shift references. Configured choice
uses scripted local HTTPS model replies; this is not an external model, deployed
API or accuracy qualification.
The installed-core runner copies consumer tests beneath tests.integration.nmrnet.
"""

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import math
import os
import sys
import tempfile
import unittest
from builtins import BaseExceptionGroup
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from tests.fakes.attempts import ATTEMPT_REF, complete_response, observation, progress_response, start_response
from tests.fakes.conversation import call
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
from nmrnet_provider.smiles import GenerationSettings
from nmrnet_provider.smiles_client import SmilesClient
from provider_core.api import ProviderAPI
from provider_core.attempt_record import StartPending, TerminalConfirmed, TerminalPending
from provider_core.attempt_request import start_body
from provider_core.attempt_store import AttemptStore
from provider_core.controller import ControllerFailed
from provider_core.ownership import AttemptClaims
from provider_core.publication import PublicationFailed
from provider_core.recovery import AttemptRecovery

# The local model fixture chooses this authored instruction's exact UTF-8 span.
INSTRUCTIONS = b"SMILES CCCC; generate 3 conformers."
SETTINGS = GenerationSettings(seed=20261019, max_iterations=1280, timeout_seconds=1)


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


class ObservedGenerator(SmilesClient):
    """Retain the actual native return for later exact correspondence assertions."""

    async def generate(self, source, requested_count):
        self.calls += 1
        self.generated = await super().generate(source, requested_count)
        return self.generated


class GeneratorRetirement(HostRetirement):
    confirmed = False

    async def retire(self):
        await super().retire()
        if Path("/generation-ipc/geometry.sock").exists() or Path("/generation-ipc/ready").exists():
            raise AssertionError("Generator retirement preceded removal of its private IPC grants")
        self.confirmed = True


class GeneratedPredictionClient(CheckedClient):
    async def prepare(self, requests):
        if not self.generator_host.confirmed:
            raise AssertionError("Scientific dispatch preceded confirmed generator retirement")
        return await super().prepare(requests)


@unittest.skipUnless(
    os.environ.get("NMRNET_LIVE_SMILES_WORKER") == "1", "run make test/integration/provider-scientific-worker/smiles"
)
class LiveSmilesLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_batch_is_retired_predicted_retained_and_replayed_without_rerun(self):
        async with asyncio.timeout(150):
            await self.run_lifecycle()

    async def allocate_generator(self, *, settings, operation_seconds):
        self.assertEqual(settings, SETTINGS)
        self.assertEqual(operation_seconds, 5)
        self.assertFalse(hasattr(self, "generator"), "This fixture permits one native generation allocation")
        allocation = os.environ["NMRNET_QUALIFICATION_GENERATOR_ALLOCATION"]
        self.assertEqual(Path("/generation-ipc/ready").read_bytes(), (allocation + "\n").encode())
        reader, writer = await asyncio.open_unix_connection("/generation-ipc/geometry.sock")
        self.generator = ObservedGenerator(reader, writer, allocation=allocation, settings=settings, operation_seconds=5)
        self.generator.calls = 0
        return self.generator, self.generator_host

    async def model_reply(self, request, raw):
        self.assertIn(b"authorization: bearer private_model_key\r\n", request.lower())
        self.assertNotIn(b"signature:", request.lower())
        self.model_calls += 1
        messages = json.loads(raw)["messages"]
        self.assertEqual(messages[1]["content"], INSTRUCTIONS.decode())
        if messages[-1]["role"] == "tool":
            self.assertTrue(self.generator_host.confirmed, "Prepared reference preceded generator retirement")
            reference = json.loads(messages[-1]["content"])["reference"]
            name, arguments = "submit", {"reference": reference}
        else:
            name, arguments = "prepare_smiles", {"start_byte": 7, "end_byte": 11, "requested_count": 3}
        return json.dumps({"choices": [{"message": call(name, name, arguments), "finish_reason": "tool_calls"}]}).encode()

    @asynccontextmanager
    async def configured_peers(self):
        async with (
            TlsPeer("success", response_factory=self.reply) as peer,
            TlsPeer("success", response_factory=self.model_reply) as model,
        ):
            with tempfile.TemporaryDirectory() as directory:
                files = ConfigFiles(
                    directory,
                    api_origin=peer.origin,
                    model_origin=model.origin,
                    ca_file=Path(peer.scratch.name) / "cert.pem",
                    model_ca_file=Path(model.scratch.name) / "cert.pem",
                )
                raw = files.raw.replace(b"seed = 17", f"seed = {SETTINGS.seed}".encode())
                raw = raw.replace(b"max_iterations = 200", f"max_iterations = {SETTINGS.max_iterations}".encode())
                raw = raw.replace(json.dumps(str(files.state)).encode(), b'"/state/attempts"')
                raw = raw.replace(b"max_worker_calls = 2", b"max_worker_calls = 1")
                files.write(raw)
                self.config = load_provider_config(files.path)
                self.key = self.config.api.identity.private_key
                self.model_peer = model
                yield peer

    async def run_lifecycle(self):
        self.counts, self.model_calls = Counter(), 0
        self.allocation = os.environ["NMRNET_QUALIFICATION_ALLOCATION"]
        self.generator_host = GeneratorRetirement(
            allocation=os.environ["NMRNET_QUALIFICATION_GENERATOR_ALLOCATION"],
            request_directory=Path("/control/generation-request"),
            receipt_directory=Path("/control/generation-receipt"),
            operation_seconds=30,
        )
        self.retirement = PrestartedSourcesRetirement(
            source_hosts=(self.generator_host,),
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

            await settle_fixture((self.generator_host.retire, retire_science_host), primary=sys.exception())

    async def joined_lifecycle(self):
        fixture_bytes = Path("/input/smiles.json").read_bytes()
        self.fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
        (self.case,) = [item for item in json.loads(fixture_bytes)["normative"] if item["name"] == "flexible_multiple"]
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
                client = ProviderAPI(self.config.api.identity, transport)
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
                self.worker = GeneratedPredictionClient(reader, writer, allocation=self.allocation, operation_seconds=90)
                self.worker.store = self.store
                self.worker.generator_host = self.generator_host
                root = Path(__file__).parents[1]
                self.execution = qualification_execution_identity(
                    (
                        ("tests/integration/provider_core/normative/test_nmrnet_smiles_lifecycle.py", Path(__file__)),
                        (
                            "tests/integration/provider_core/adversarial/test_nmrnet_smiles_lifecycle.py",
                            root / "adversarial/test_nmrnet_smiles_lifecycle.py",
                        ),
                        ("tests/integration/provider_core/normative/__init__.py", Path(__file__).parent / "__init__.py"),
                        ("tests/integration/provider_core/adversarial/__init__.py", root / "adversarial/__init__.py"),
                        ("tests/integration/provider_worker/smiles_probe.py", Path("/source/smiles_probe.py")),
                        ("tests/integration/provider_worker/retirement_probe.py", Path("/workspace/probe.py")),
                        ("requirements/preparation.lock", Path("/input/preparation.lock")),
                        ("tests/fixtures/preparation/smiles.json", Path("/input/smiles.json")),
                        ("tests/integration/provider_core/config_ports.py", root / "config_ports.py"),
                        ("tools/geometry_image.sh", Path("/consumer/tools/geometry_image.sh")),
                    )
                )
                self.execution.update(
                    generator_image_id=os.environ["NMRNET_QUALIFICATION_GENERATOR_IMAGE_ID"],
                    controller_image_id=os.environ["NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID"],
                    startup_configuration_sha256=self.config.source_sha256,
                )
                require_host_source_closure(self.execution, Path("/input/sources.sha256").read_bytes())
                controller = await assemble_attempt(
                    config=self.config,
                    pending=pending,
                    api=client,
                    store=self.store,
                    claims=AttemptClaims(),
                    worker=self.worker,
                    retirement=self.retirement,
                    execution_identity=self.execution,
                    selected_artifacts=self.artifacts,
                    runtime_expectations=metadata["runtime"],
                    allocate_reader=AsyncMock(side_effect=AssertionError("SMILES fixture must not allocate a reader")),
                    allocate_generator=self.allocate_generator,
                )
                with self.assertRaises(ControllerFailed) as lost:
                    await controller.run(pending)
                self.assertIsInstance(lost.exception.cause, PublicationFailed)
                self.assertTrue(lost.exception.cause.api_failure.attempt_mutation_may_have_committed)
                (terminal,) = await self.store.records()
                self.assertIs(type(terminal), TerminalPending)
                self.assertEqual(terminal, self.retirement.retained)
                self.assertTrue(self.generator_host.confirmed and self.retirement.confirmed)
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
            self.assertEqual(self.model_calls, 2)
            self.assertEqual(self.generator.calls, 1)
            self.assertEqual((self.worker.preparations, self.worker.executions), (1, 1))
            for name in ("jobs", "start", "input", "preparing", "running"):
                self.assertEqual(self.counts[name], 1)
            self.assertEqual(self.counts["complete"], 2)
            self.assertNotIn("rdkit", sys.modules)
            self.assertNotIn("torch", sys.modules)
        content = base64.b64decode(json.loads(confirmed.pending.body)["canonical_result_base64"], validate=True)
        # This successful finite fixture has exact generated coordinates. Keep
        # its exact published result for review, rather than disposable journals.
        Path("/state/scientific-result.json").write_bytes(content)
        Path("/state/qualification.json").write_text(
            json.dumps(
                {
                    "schema_id": "nmrnet.generated_lifecycle.qualification.v1",
                    "scope": "configured assembly and native generation/released H/C through local signed TLS/store and scripted HTTPS model replies; not external model, deployed API or accuracy",
                    "fixture_sha256": self.fixture_sha256,
                    "input_fingerprint": facts(INSTRUCTIONS).input_fingerprint,
                    "generation": json.loads(content)["generation"],
                    "generator_image_id": os.environ["NMRNET_QUALIFICATION_GENERATOR_IMAGE_ID"],
                    "scientific_image_id": os.environ["NMRNET_QUALIFICATION_IMAGE_ID"],
                    "controller_image_id": os.environ["NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID"],
                    "installed_core_version": importlib.metadata.version("provider-core"),
                    "conformers": [
                        {
                            "conformer_id": item.conformer_id,
                            "request_sha256": hashlib.sha256(item.request_bytes).hexdigest(),
                            "stereochemistry_sha256": hashlib.sha256(item.stereochemistry).hexdigest(),
                        }
                        for item in self.generator.generated.conformers
                    ],
                    "runtime": self.scientific_runtime,
                    "selected_artifacts": self.artifacts,
                    "scientific_result": {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)},
                    "generation_calls": self.generator.calls,
                    "model_exchanges": self.model_calls,
                    "interpretation": json.loads(content)["interpretation"],
                    "scientific_preparations": self.worker.preparations,
                    "scientific_executions": self.worker.executions,
                    "api_operations": dict(self.counts),
                    "host_retirement_confirmed": {
                        "generator": self.generator_host.confirmed,
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
            client = ProviderAPI(self.config.api.identity, transport)
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
        verify_signed_request(request, body, origin=self.peer.origin, key=self.key)
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        if path == "/provider/v1/jobs":
            self.assertEqual(method, "GET")
            self.counts["jobs"] += 1
            document = feed_page()
            document.update(analysis_kind_ref=ANALYSIS_KIND_REF, next_cursor=None)
            document["jobs"] = [
                {
                    **asdict(facts(INSTRUCTIONS)),
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
            return response(INSTRUCTIONS)
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF + "/progress":
            self.assertEqual(method, "PUT")
            phase = json.loads(body)["phase"]
            self.assertIn(phase, {"preparing", "running"})
            self.counts[phase] += 1
            return progress_response(phase=phase)
        self.assertEqual(path, "/provider/v1/execution-attempts/complete")
        self.assertEqual(method, "POST")
        self.assertTrue(self.generator_host.confirmed and self.retirement.confirmed)
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

    def assert_result(self, terminal):
        native = self.generator.generated
        metadata = json.loads(native.provenance)
        self.assertEqual(metadata["source_sha256"], hashlib.sha256(self.case["smiles"].encode()).hexdigest())
        self.assertEqual(metadata["requested_count"], self.case["requested_conformers"])
        self.assertEqual([atom["element"] for atom in metadata["source_atoms"]], self.case["source_atoms"])
        self.assertEqual(metadata["added_hydrogen_parents"], self.case["expected_added_hydrogen_parents"])
        self.assertEqual(len(native.conformers), 3)
        content = base64.b64decode(json.loads(terminal.body)["canonical_result_base64"], validate=True)
        result = json.loads(content)
        self.assertEqual(result["execution"], self.execution)
        self.assertEqual(result["selected_models"], self.artifacts)
        self.assertEqual(result["runtime"], self.scientific_runtime)
        identity = json.loads(terminal.attempt.prepared_identity.content)
        self.assertEqual(identity, json.loads(self.worker.prepared_identity.content))
        self.assertEqual(result["generation"]["provenance"], metadata)
        self.assertEqual(
            result["generation"]["selection"],
            {
                "job_ref": "job:example",
                "input_fingerprint": facts(INSTRUCTIONS).input_fingerprint,
                "start_byte": 7,
                "end_byte": 11,
                "source_sha256": hashlib.sha256(b"CCCC").hexdigest(),
                "requested_count": 3,
            },
        )
        self.assertEqual(result["generation"]["provenance_sha256"], hashlib.sha256(native.provenance).hexdigest())
        self.assertEqual(identity["generation"], {key: result["generation"][key] for key in ("selection", "provenance_sha256")})
        evidence = json.loads(terminal.evidence.content)
        for item, geometry, observed in zip(native.conformers, result["geometries"], evidence["geometries"], strict=True):
            request = json.loads(item.request_bytes)
            self.assertEqual(Counter(request["atoms"]), self.case["generated_atom_counts"])
            self.assertEqual(
                request["atoms"], self.case["source_atoms"] + ["H"] * len(self.case["expected_added_hydrogen_parents"])
            )
            self.assertEqual(geometry["prepared_request"], request)
            self.assertEqual(geometry["source"]["conformer_id"], item.conformer_id)
            self.assertEqual(geometry["preparation"]["stereochemistry"], json.loads(item.stereochemistry))
            self.assertEqual(geometry["preparation"]["stereochemistry_sha256"], hashlib.sha256(item.stereochemistry).hexdigest())
            self.assertEqual(geometry["request_sha256"], hashlib.sha256(item.request_bytes).hexdigest())
            self.assertEqual(observed["request_sha256"], geometry["request_sha256"])
            self.assertEqual(observed["provenance"]["artifacts"], result["selected_models"])
            self.assertEqual(observed["provenance"]["runtime"], result["runtime"])
            self.assertEqual(set(geometry["loaded_elements"]), {"H", "C"})
            self.assertEqual(
                [(row["atom_index"], row["element"]) for row in geometry["predictions"]], list(enumerate(request["atoms"]))
            )
            self.assertTrue(all(math.isfinite(row["shift_ppm"]) for row in geometry["predictions"]))
