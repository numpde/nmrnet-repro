"""Native CCCC/N=3 generation through released science and local signed publication.

The chemistry oracle is smiles.json normative.flexible_multiple: atom inventory
and literal H-parent pairs, without coordinate/shift references. Choice is scripted;
the loopback peer proves wire/lifecycle behavior, not a deployed API or accuracy.
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
import unittest
from builtins import BaseExceptionGroup
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, complete_response, observation, progress_response, start_response
from tests.fakes.job_input import facts, response
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.test_live_worker import (
    CheckedClient,
    CheckedRetirement,
    qualification_execution_identity,
    verify_signed_request,
)

from nmrnet_provider.input_preparation import InputPreparation
from nmrnet_provider.retirement import HostRetirement
from nmrnet_provider.session import NMRNetSession
from nmrnet_provider.smiles import GenerationSettings
from nmrnet_provider.smiles_client import SmilesClient
from provider_core.api import ProviderAPI
from provider_core.attempt_record import MAX_RECORD_BYTES, TerminalConfirmed, TerminalPending
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.controller import AttemptController, ControllerFailed
from provider_core.ownership import AttemptClaims
from provider_core.publication import PublicationFailed
from provider_core.recovery import AttemptRecovery
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport

# Authored instruction/span mapping exercises binding, not interpretation of N.
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
    if failures:
        causes = []
        if primary is not None:
            causes.append(primary)
        causes.extend(failures)
        combined = BaseExceptionGroup("Native lifecycle qualification cleanup failed", causes)
        for cause in causes:
            if isinstance(cause, asyncio.CancelledError):
                raise cause from combined
        raise combined


class PrestartedGeneratorInput(InputPreparation):
    """The fixture additionally owns the host's already-started native endpoint.

    Normal InputPreparation owns allocations returned by its factory. This fixed
    proof also has an endpoint when setup/choice fails before that factory runs.
    Settle the normal owner first, then retire the extra prestarted grant before
    Session proceeds to the independent prediction worker.
    """

    def __init__(self, *, generator_host, **configuration):
        super().__init__(**configuration)
        self.generator_host = generator_host

    async def retire(self):
        await settle_fixture((super().retire, self.generator_host.retire), primary=None)


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

    async def choose(self, instructions, capability):
        self.assertEqual(instructions.specification_bytes, INSTRUCTIONS)
        self.choice_calls += 1
        reference = await capability.prepare_smiles(start_byte=7, end_byte=11, requested_count=3)
        self.assertTrue(self.generator_host.confirmed, "Prepared reference preceded generator retirement")
        capability.submit(reference)

    async def run_lifecycle(self):
        self.counts, self.choice_calls = Counter(), 0
        self.key = Ed25519PrivateKey.generate()
        self.allocation = os.environ["NMRNET_QUALIFICATION_ALLOCATION"]
        self.generator_host = GeneratorRetirement(
            allocation=os.environ["NMRNET_QUALIFICATION_GENERATOR_ALLOCATION"],
            request_directory=Path("/control/generation-request"),
            receipt_directory=Path("/control/generation-receipt"),
            operation_seconds=30,
        )
        self.retirement = CheckedRetirement(
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
        async with TlsPeer("success", response_factory=self.reply) as peer:
            self.peer = peer
            recovery_scope = replace(scope(), api_origin=peer.origin, generation_id=self.allocation)
            pending = replace(record(), scope=recovery_scope, input=facts(INSTRUCTIONS))
            transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=1)
            client = ProviderAPI(SigningIdentity(peer.origin, "credential:test", self.key), transport)
            store_path = Path("/state/attempts")
            store_path.mkdir(mode=0o700)
            self.store = self.open_store(store_path, recovery_scope)
            await self.store.open()
            self.retirement.store = self.store
            session = controller = preparation = None
            self.source_transports = ()
            try:
                # Disjoint source transports are required owners even though the
                # scripted SMILES route never acquires an Upload in this fixture.
                self.source_transports = tuple(
                    HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=1) for _ in range(2)
                )
                preparation = PrestartedGeneratorInput(
                    generator_host=self.generator_host,
                    uploads=SimpleNamespace(job_ref="job:example"),
                    source_api_transport=self.source_transports[0],
                    source_store_transport=self.source_transports[1],
                    allocate_reader=AsyncMock(side_effect=AssertionError("SMILES fixture must not allocate a reader")),
                    allocate_generator=self.allocate_generator,
                    generation_settings=SETTINGS,
                    generation_seconds=5,
                    chooser=SimpleNamespace(run=self.choose),
                    max_worker_calls=1,
                    seconds=45,
                )
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
                        ("tools/geometry_image.sh", Path("/consumer/tools/geometry_image.sh")),
                    )
                )
                self.execution.update(
                    generator_image_id=os.environ["NMRNET_QUALIFICATION_GENERATOR_IMAGE_ID"],
                    controller_image_id=os.environ["NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID"],
                )
                require_host_source_closure(self.execution, Path("/input/sources.sha256").read_bytes())
                session = NMRNetSession(
                    worker=self.worker,
                    retirement=self.retirement,
                    execution_identity=self.execution,
                    selected_artifacts=self.artifacts,
                    runtime_expectations=metadata["runtime"],
                    input_preparation=preparation,
                )
                controller = AttemptController(
                    client,
                    self.store,
                    AttemptClaims(),
                    session,
                    allowed_job_states=frozenset({"open"}),
                    poll_seconds=0.2,
                    task_settle_seconds=1,
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
                if session is not None:
                    operations.append(session.retire)
                elif preparation is not None:
                    operations.append(preparation.retire)
                operations.append(self.retirement.retire)
                operations.extend(port.retire for port in self.source_transports)
                operations.extend((self.store.close, transport.retire))
                await settle_fixture(operations, primary=primary)
            confirmed = await self.replay_terminal(store_path, recovery_scope, terminal)
            self.assertEqual(self.choice_calls, 1)
            self.assertEqual(self.generator.calls, 1)
            self.assertEqual((self.worker.preparations, self.worker.executions), (1, 1))
            for name in ("start", "input", "preparing", "running"):
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
                    "scope": "native generation and released H/C through local signed TLS/store; scripted choice, not deployed API or accuracy",
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
        return AttemptStore(path, scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=2)

    async def replay_terminal(self, path, recovery_scope, terminal):
        self.store = self.open_store(path, recovery_scope)
        await self.store.open()
        transport = HttpTransport(trust=self.peer.trust, exchange_seconds=2, retirement_seconds=1)
        client = ProviderAPI(SigningIdentity(self.peer.origin, "credential:test", self.key), transport)
        try:
            self.assertEqual(await self.store.records(), (terminal,))
            (claim,) = AttemptClaims().claim_records((terminal,))
            try:
                confirmed = await AttemptRecovery(client, self.store, claim).recover(terminal)
                self.assertIs(type(confirmed), TerminalConfirmed)
                self.assertTrue(confirmed.receipt.replayed)
                self.assertEqual(confirmed.pending, terminal)
                self.assertEqual(await self.store.records(), (confirmed,))
            finally:
                claim.release()
        finally:
            await self.store.close()
            await transport.retire()
        reopened = self.open_store(path, recovery_scope)
        await reopened.open()
        try:
            self.assertEqual(await reopened.records(), (confirmed,))
        finally:
            await reopened.close()
        return confirmed

    async def reply(self, request, body):
        self.assertLess(sum(self.counts.values()), 600, "Qualification request budget exhausted")
        verify_signed_request(request, body, origin=self.peer.origin, key=self.key)
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        if path == "/provider/v1/execution-attempts/start":
            self.assertEqual(method, "POST")
            (retained,) = await self.store.records()
            self.assertEqual(retained.body, body)
            self.counts["start"] += 1
            return start_response()
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
