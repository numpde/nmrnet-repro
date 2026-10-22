"""Actual service and lazy native allocations for the three qualified input routes.

Local signed API/store and scripted model peers qualify this composition only.
Independent molecular/source oracles stay in SuppliedLifecycle/SmilesLifecycle; neither
native outputs nor this test can regenerate it. No deployed/external claim.
"""

import asyncio
import base64
import hashlib
import json
import os
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.fakes.attempt_inventory import response as inventory_response
from tests.fakes.hello import receipt
from tests.integration.nmrnet.capacity_service import CapacityServiceLifecycle
from tests.integration.nmrnet.formats_service import FORMAT_CASES, FormatsServiceLifecycle
from tests.integration.nmrnet.native_lifecycle import PreparedPredictionClient, require_host_source_closure, settle_fixture
from tests.integration.nmrnet.normative.test_nmrnet_smiles_lifecycle import ObservedGenerator, SmilesLifecycle
from tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle import ObservedReader, SuppliedLifecycle
from tests.integration.nmrnet.reference_service import ReferenceServiceLifecycle
from tests.integration.nmrnet.resource_evidence import require_cgroup_resources, retired_worker_resources
from tests.integration.nmrnet.test_live_worker import qualification_execution_identity, verify_signed_request

from nmrnet_provider.admission import AdmissionHeld, AdmissionPolicy
from nmrnet_provider.allocation import WorkerAllocation
from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.service import Announcement, ProviderService, ServiceFailed, ServicePolicy
from provider_core.api import ProviderAPI
from provider_core.attempt_record import TerminalPending
from provider_core.controller import ControllerFailed
from provider_core.hello import AnalysisOffering
from provider_core.ownership import AttemptClaims
from provider_core.publication import PublicationFailed
from tools.container_resources import cgroup_snapshot


class ObservedAllocation(WorkerAllocation):
    confirmed = False

    async def retire(self):
        await super().retire()
        if self.ipc.exists():
            raise AssertionError("Native retirement preceded removal of the exact IPC grant")
        self.confirmed = True


class NativeServiceLifecycle:
    """One service/allocation/replay witness; route fixtures retain their own oracles."""

    maximum_records = 2

    async def check_service(self):
        async with asyncio.timeout(180):
            await self.run_service()

    def fixture_sources(self):
        """Bind the route's independent oracle files into executed-source evidence."""
        if self.input_kind == "smiles":
            fixture_sources = [("tests/fixtures/preparation/smiles.json", Path("/input/smiles.json"))]
        else:
            fixture_sources = [("tests/fixtures/preparation/expected.json", Path("/input/expected.json"))]
            if self.input_kind == "upload":
                fixture_sources.append(
                    ("tests/fixtures/preparation/normative/mixed_records.xyz", Path("/input/mixed_records.xyz"))
                )
        return fixture_sources

    async def run_service(self):
        self.counts, self.model_calls, self.events = Counter(), 0, []
        self.observed_allocations = []
        self.source_clients = []
        self.load_case()
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
        self.execution = qualification_execution_identity(
            (
                ("tests/integration/provider_core/native_lifecycle.py", Path(__file__).parents[1] / "native_lifecycle.py"),
                ("tests/integration/provider_core/config_ports.py", Path(__file__).parents[1] / "config_ports.py"),
                (
                    "tests/integration/provider_core/normative/test_nmrnet_supplied_lifecycle.py",
                    Path(__file__).with_name("test_nmrnet_supplied_lifecycle.py"),
                ),
                (
                    "tests/integration/provider_core/normative/test_nmrnet_smiles_lifecycle.py",
                    Path(__file__).with_name("test_nmrnet_smiles_lifecycle.py"),
                ),
                ("tests/integration/provider_core/normative/test_nmrnet_native_service.py", Path(__file__)),
                ("tests/integration/provider_core/normative/__init__.py", Path(__file__).with_name("__init__.py")),
                *self.fixture_sources(),
                ("tools/provider_workers.sh", Path("/consumer/tools/provider_workers.sh")),
                ("tools/provider_service_qualification.sh", Path("/consumer/tools/provider_service_qualification.sh")),
                ("tools/geometry_image.sh", Path("/consumer/tools/geometry_image.sh")),
                (
                    "tests/integration/provider_core/resource_evidence.py",
                    Path(__file__).parents[1] / "resource_evidence.py",
                ),
                (
                    "tests/integration/provider_core/adversarial/__init__.py",
                    Path(__file__).parents[1] / "adversarial/__init__.py",
                ),
            )
        )
        async with self.configured_peers() as peer:
            self.peer = peer
            self.execution.update(
                geometry_image_id=os.environ["NMRNET_QUALIFICATION_GEOMETRY_IMAGE_ID"],
                controller_image_id=os.environ["NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID"],
                startup_configuration_sha256=self.config.source_sha256,
            )
            require_host_source_closure(self.execution, Path("/input/sources.sha256").read_bytes())
            scope = self.config.lifecycle.scope
            self.config.lifecycle.store_root.mkdir(mode=0o700)
            self.store = self.open_store(self.config.lifecycle.store_root, scope)
            transport = self.config.api.http.new_transport()
            service = None
            factory = None
            try:
                await self.store.open()
                api = ProviderAPI(self.config.api.identity, transport, expected_topology=self.config.api.expected_topology)
                factory = self.factory = NativeAttemptFactory(
                    config=self.config,
                    api=api,
                    store=self.store,
                    claims=AttemptClaims(),
                    execution_identity=self.execution,
                    selected_artifacts=self.artifacts,
                    policy=NativePolicy(allocation_seconds=30, prediction_seconds=90, source_seconds=10),
                )
                original_reader = self.factory.allocate_reader
                original_generator = self.factory.allocate_generator

                async def allocate_reader():
                    self.assertIn(self.input_kind, ("pasted", "upload"))
                    client, retirement = await original_reader()
                    self.geometry_client, self.geometry_host = client, retirement
                    self.source_clients.append((client, retirement))
                    client.calls = 0
                    self.worker.geometry_host = retirement
                    return client, retirement

                async def allocate_generator(*, settings, operation_seconds):
                    self.assertEqual(self.input_kind, "smiles")
                    self.assertEqual(settings, self.generation_settings)
                    self.assertEqual(operation_seconds, 5)
                    client, retirement = await original_generator(settings=settings, operation_seconds=operation_seconds)
                    self.geometry_client, self.geometry_host = client, retirement
                    client.calls = 0
                    self.worker.geometry_host = retirement
                    return client, retirement

                self.factory.allocate_reader = allocate_reader
                self.factory.allocate_generator = allocate_generator

                def requested_allocation(**arguments):
                    allocation = ObservedAllocation(**arguments)
                    self.observed_allocations.append(allocation)
                    if isinstance(self, FormatsServiceLifecycle):
                        self.retain_allocation_intent(allocation)
                    return allocation

                def prediction_client(*arguments, **options):
                    self.worker = PreparedPredictionClient(*arguments, **options)
                    self.worker.store = self.store
                    return self.worker

                async def open_attempt(pending):
                    self.events.append("factory")
                    self.assertEqual(self.events[:2], ["inventory", "hello"])
                    self.assertEqual(
                        [event for event in self.events if event != "hello"], ["inventory", "inventory", "jobs", "factory"]
                    )
                    self.selected_input = pending.input
                    (self.state_root / "selected").write_bytes(pending.input.input_fingerprint.encode() + b"\n")
                    controller = await self.factory(pending)
                    try:
                        (self.retirement,) = self.observed_allocations
                        self.assertEqual(self.factory.allocations, [])
                        self.assertEqual(self.factory.clients, {})
                        self.scientific_runtime = json.loads(self.retirement.metadata)["runtime"]
                        return controller
                    except BaseException:
                        await settle_fixture((controller.settle,), primary=sys.exception())
                        raise

                snapshot = Announcement(
                    "Native local fixture", "NMRNet coordinate shifts", (AnalysisOffering("peaks_from_mol", "Coordinate shifts"),)
                )
                service = ProviderService(
                    config=self.config,
                    store=self.store,
                    api=api,
                    open_attempt=open_attempt,
                    current_announcement=lambda: snapshot,
                    on_started=self.started,
                    on_api_retry=lambda _kind, _failure, _seconds: self.fail("Unexpected API retry in this scientific witness"),
                    on_api_recovered=lambda _kind, _failure: self.fail("Unexpected API recovery in this scientific witness"),
                    policy=ServicePolicy(
                        AdmissionPolicy(1, 2),
                        idle_seconds=0.2,
                        hello_seconds=2,
                        drain_seconds=1,
                        settle_seconds=5,
                        api_backoff_max_seconds=30,
                    ),
                )
                with (
                    patch("nmrnet_provider.native_factory.WorkerAllocation", side_effect=requested_allocation),
                    patch("nmrnet_provider.native_factory.PredictionClient", side_effect=prediction_client),
                    patch("nmrnet_provider.native_factory.GeometryClient", ObservedReader),
                    patch("nmrnet_provider.native_factory.SmilesClient", ObservedGenerator),
                    self.assertRaises(ServiceFailed) as lost,
                ):
                    await service.run(asyncio.Event())
                work = [failure for operation, failure in lost.exception.failures if operation == "Attempt work"]
                self.assertEqual(len(work), 1)
                self.assertIsInstance(work[0], ControllerFailed)
                self.assertIsInstance(
                    work[0].cause,
                    PublicationFailed,
                    json.dumps(failure_evidence(lost.exception, secrets=("private_model_key",))),
                )
                (terminal,) = await self.store.records()
                self.assertIs(type(terminal), TerminalPending)
                self.assertTrue(self.geometry_host.confirmed and self.retirement.confirmed)
                self.assertEqual((self.worker.preparations, self.worker.executions), (1, 1))
                self.assertEqual(len(self.observed_allocations), 1 + self.expected_preparation_calls)
                self.assertEqual(
                    [item.role for item in self.observed_allocations],
                    ["prediction"] + ["smiles" if self.input_kind == "smiles" else "reader"] * self.expected_preparation_calls,
                )
                self.assertEqual(self.factory.allocations, [])
                self.assertEqual(self.factory.clients, {})
                self.assertEqual(self.factory.channel_closures, {})
                self.assertEqual(self.geometry_client.calls, 1)
                for client, retirement in self.source_clients:
                    self.assertEqual(client.calls, 1)
                    self.assertTrue(retirement.confirmed)
                self.assertEqual(self.model_calls, self.expected_model_calls)
                if self.input_kind != "upload":
                    self.assertEqual((self.counts["membership"], self.counts["grant"]), (0, 0))
                self.assertEqual(self.counts["complete"], 1)
                requests_before = len(peer.requests)
                replacement = ProviderService(
                    config=self.config,
                    store=self.store,
                    api=api,
                    open_attempt=open_attempt,
                    current_announcement=lambda: snapshot,
                    on_started=self.started,
                    on_api_retry=lambda _kind, _failure, _seconds: self.fail("Unexpected API retry in this scientific witness"),
                    on_api_recovered=lambda _kind, _failure: self.fail("Unexpected API recovery in this scientific witness"),
                    policy=service.policy,
                )
                with self.assertRaises(ServiceFailed) as refused:
                    await replacement.run(asyncio.Event())
                self.assertIsInstance(refused.exception.cause, AdmissionHeld)
                self.assertEqual(await self.store.records(), (terminal,))
                await replacement.settle()
                self.assertEqual(len(peer.requests), requests_before)
                self.assertEqual(len(self.observed_allocations), 1 + self.expected_preparation_calls)
            finally:
                operations = [self.settle_preflight, self.store.close, transport.retire]
                if service is not None:
                    operations.insert(0, service.settle)
                if factory is not None:
                    operations.insert(0, factory.settle)
                await settle_fixture(operations, primary=sys.exception())
            confirmed = await self.replay_terminal(self.config.lifecycle.store_root, scope, terminal)
            self.assertEqual(confirmed.pending, terminal)
            self.assertEqual(self.counts["complete"], 2)
            self.assertEqual((self.worker.preparations, self.worker.executions), (1, 1))
            self.assertNotIn("rdkit", sys.modules)
            self.assertNotIn("torch", sys.modules)
        content = base64.b64decode(json.loads(confirmed.pending.body)["canonical_result_base64"], validate=True)
        (self.state_root / "scientific-result.json").write_bytes(content)
        native_resources = [
            retired_worker_resources(Path("/native/receipts") / item.allocation, role=item.role, allocation=item.allocation)
            for item in self.observed_allocations
        ]
        controller_resources = require_cgroup_resources(cgroup_snapshot(), memory_bytes=536870912)
        summary = {
            "scope": "actual ProviderService and production factory/worker entrypoints with post-admission host allocation; local scripted TLS API/model/store peers, not external/deployed workflow or accuracy",
            "input_kind": self.input_kind,
            "events": self.events,
            "scientific_executions": self.worker.executions,
            "scientific_result": {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)},
            "api_operations": dict(self.counts),
            "terminal_replayed_after_reopen": True,
            "allocations": [
                {"role": item.role, "token": item.allocation, "host_retired": item.confirmed}
                for item in self.observed_allocations
            ],
            "execution": self.execution,
            "process_resources": {
                "native": native_resources,
                "controller": {
                    "scope": "cgroup_through_post_replay_and_peer_cleanup_snapshot_including_package_installation",
                    "cgroup": controller_resources,
                },
                "scope": "Separate container observations through their sampling instants; no simultaneous aggregate or full-lifetime peak claim.",
            },
            **self.qualification_details(content),
        }
        (self.state_root / "qualification.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        (self.state_root / "finished").write_text("qualified\n")

    def started(self):
        (self.state_root / "admitted").write_text("admitted\n")

    async def reply(self, request, body):
        path = urlsplit(request.split(b"\r\n", 1)[0].decode().split()[1]).path
        if path in ("/provider/v1/hello", "/provider/v1/execution-attempts"):
            verify_signed_request(request, body, origin=self.peer.origin, key=self.key)
            self.assertNotIn(b"bearer ", request.lower())
            self.assertNotIn(b"private_model_key", request.lower())
            name = "hello" if path.endswith("/hello") else "inventory"
            self.events.append(name)
            self.counts[name] += 1
            return receipt() if name == "hello" else inventory_response(attempts=[], next_cursor=None)
        if path == "/provider/v1/jobs":
            self.events.append("jobs")
        return await super().reply(request, body)


@unittest.skipUnless(
    os.environ.get("NMRNET_LIVE_NATIVE_SERVICE") == "upload", "run make test/integration/provider-service/uploads"
)
class NativeUploadServiceTests(NativeServiceLifecycle, SuppliedLifecycle, unittest.IsolatedAsyncioTestCase):
    input_kind = "upload"

    async def test_service_upload_records_reach_prediction_and_exact_replay(self):
        await self.check_service()


@unittest.skipUnless(
    os.environ.get("NMRNET_LIVE_NATIVE_SERVICE") == "pasted", "run make test/integration/provider-service/pasted"
)
class NativePastedServiceTests(NativeServiceLifecycle, SuppliedLifecycle, unittest.IsolatedAsyncioTestCase):
    input_kind = "pasted"

    async def test_service_pasted_nm_coordinates_reach_prediction_and_exact_replay(self):
        await self.check_service()


@unittest.skipUnless(
    os.environ.get("NMRNET_LIVE_NATIVE_SERVICE") == "smiles", "run make test/integration/provider-service/smiles"
)
class NativeSmilesServiceTests(NativeServiceLifecycle, SmilesLifecycle, unittest.IsolatedAsyncioTestCase):
    async def test_service_generated_conformers_reach_prediction_and_exact_replay(self):
        await self.check_service()


@unittest.skipUnless(
    os.environ.get("NMRNET_LIVE_NATIVE_SERVICE") == "references", "run make test/integration/provider-service/references"
)
class NativeReferenceServiceTests(NativeServiceLifecycle, ReferenceServiceLifecycle, unittest.IsolatedAsyncioTestCase):
    fixture_sources = ReferenceServiceLifecycle.fixture_sources

    async def test_four_reference_inputs_match_every_prediction_and_historical_position(self):
        await self.check_service()


@unittest.skipUnless(
    os.environ.get("NMRNET_LIVE_NATIVE_SERVICE") == "capacity", "run make test/integration/provider-service/capacity"
)
class NativeCapacityServiceTests(NativeServiceLifecycle, CapacityServiceLifecycle, unittest.IsolatedAsyncioTestCase):
    fixture_sources = CapacityServiceLifecycle.fixture_sources

    async def test_128_atoms_per_head_reach_prediction_and_exact_replay_with_measured_resources(self):
        await self.check_service()


@unittest.skipUnless(
    os.environ.get("NMRNET_LIVE_NATIVE_SERVICE") == "formats", "run make test/integration/provider-service/formats"
)
class NativeFormatsServiceTests(NativeServiceLifecycle, FormatsServiceLifecycle, unittest.IsolatedAsyncioTestCase):
    fixture_sources = FormatsServiceLifecycle.fixture_sources

    async def test_selected_supplied_formats_share_one_host_without_repeating_completed_science(self):
        root = Path("/state")
        (root / "formats").mkdir(mode=0o700)
        cases, allocations = [], []
        selected = os.environ["NMRNET_QUALIFICATION_FORMAT_CASES"].split()
        self.assertTrue(selected and len(set(selected)) == len(selected) and all(name in FORMAT_CASES for name in selected))
        expected_allocations = 0
        for name in selected:
            self.format_case = name
            self.state_root = root / "formats" / name
            self.state_root.mkdir(mode=0o700)
            (self.state_root / "allocation-intents").mkdir(mode=0o700)
            (root / "active-case").write_bytes(name.encode("ascii") + b"\n")
            await self.check_service()
            expected_allocations += 1 + self.expected_preparation_calls
            self.assertTrue(all(item.confirmed for item in self.observed_allocations))
            summary = json.loads((self.state_root / "qualification.json").read_bytes())
            content = (self.state_root / "scientific-result.json").read_bytes()
            cases.append(
                {
                    "case": name,
                    "source_sha256": self.fixture_sha256,
                    "source_bytes": len(self.source),
                    "qualification": summary,
                    "scientific_result": {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)},
                }
            )
            allocations.extend(item.allocation for item in self.observed_allocations)
        self.assertEqual(len(allocations), expected_allocations)
        self.assertEqual(len(set(allocations)), expected_allocations)
        self.assertEqual(sum(case["qualification"]["scientific_executions"] for case in cases), len(selected))
        (root / "qualification.json").write_text(
            json.dumps(
                {
                    "scope": "selected fixed native format cases through local scripted API/model/store peers; not real-model interpretation or deployed API",
                    "selected_cases": selected,
                    "cases": cases,
                    "native_allocations": len(allocations),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        (root / "finished").write_bytes(b"qualified\n")
