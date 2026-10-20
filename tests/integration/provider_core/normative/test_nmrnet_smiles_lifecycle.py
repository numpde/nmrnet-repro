"""Native CCCC/N=3 oracle through the shared configured lifecycle.

The atom inventory and literal H-parent oracle come from smiles.json; HTTPS model
replies are scripted. This does not qualify external model behavior or accuracy.
"""

import asyncio
import base64
import hashlib
import json
import math
import os
import unittest
from collections import Counter
from pathlib import Path

from tests.fakes.conversation import call
from tests.fakes.job_input import facts
from tests.integration.nmrnet.native_lifecycle import NativeLifecycle

from nmrnet_provider.smiles import GenerationSettings
from nmrnet_provider.smiles_client import SmilesClient


class ObservedGenerator(SmilesClient):
    """Retain the actual native return for later exact correspondence assertions."""

    async def generate(self, source, requested_count):
        self.calls += 1
        self.generated = await super().generate(source, requested_count)
        return self.generated


class SmilesLifecycle(NativeLifecycle):
    instructions = b"SMILES CCCC; generate 3 conformers."
    input_kind = "smiles"
    expected_model_calls = 1
    generation_settings = GenerationSettings(seed=20261019, max_iterations=1280, timeout_seconds=1)

    def load_case(self):
        raw = Path("/input/smiles.json").read_bytes()
        self.fixture_sha256 = hashlib.sha256(raw).hexdigest()
        (self.case,) = [item for item in json.loads(raw)["normative"] if item["name"] == "flexible_multiple"]

    def execution_sources(self):
        return [
            ("tests/integration/provider_worker/smiles_probe.py", Path("/source/structure_probe.py")),
            ("tests/fixtures/preparation/smiles.json", Path("/input/smiles.json")),
        ]

    def qualification_details(self, content):
        return {
            "generation": json.loads(content)["generation"],
            "generation_calls": self.geometry_client.calls,
            "conformers": [
                {
                    "conformer_id": item.conformer_id,
                    "request_sha256": hashlib.sha256(item.request_bytes).hexdigest(),
                    "stereochemistry_sha256": hashlib.sha256(item.stereochemistry).hexdigest(),
                }
                for item in self.geometry_client.generated.conformers
            ],
        }

    async def allocate_reader(self):
        raise AssertionError("SMILES fixture must not allocate a reader")

    async def upload_reply(self, _method, _path, _body):
        raise AssertionError("SMILES fixture must not request an Upload")

    async def allocate_generator(self, *, settings, operation_seconds):
        self.assertEqual(settings, self.generation_settings)
        self.assertEqual(operation_seconds, 5)
        self.assertFalse(hasattr(self, "geometry_client"), "This fixture permits one native generation allocation")
        allocation = os.environ["NMRNET_QUALIFICATION_GEOMETRY_ALLOCATION"]
        self.assertEqual(Path("/geometry-ipc/ready").read_bytes(), (allocation + "\n").encode())
        reader, writer = await asyncio.open_unix_connection("/geometry-ipc/geometry.sock")
        self.geometry_client = ObservedGenerator(reader, writer, allocation=allocation, settings=settings, operation_seconds=5)
        self.geometry_client.calls = 0
        return self.geometry_client, self.geometry_host

    async def model_reply(self, request, raw):
        self.assertIn(b"authorization: bearer private_model_key\r\n", request.lower())
        self.assertNotIn(b"signature:", request.lower())
        self.model_calls += 1
        messages = json.loads(raw)["messages"]
        self.assertEqual(messages[1]["content"], self.instructions.decode())
        self.assertNotEqual(messages[-1]["role"], "tool", "Successful preparation must finish without another model turn")
        name, arguments = "prepare_smiles", {"smiles": "CCCC", "requested_count": 3}
        return json.dumps({"choices": [{"message": call(name, name, arguments), "finish_reason": "tool_calls"}]}).encode()

    def assert_result(self, terminal):
        native = self.geometry_client.generated
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
                "input_fingerprint": facts(self.instructions).input_fingerprint,
                "origin": "interpreter_supplied_smiles",
                "byte_length": 4,
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


@unittest.skipUnless(os.environ.get("NMRNET_LIVE_SMILES_WORKER") == "1", "run the fixed scientific-worker/smiles lane")
class LiveSmilesLifecycleTests(SmilesLifecycle, unittest.IsolatedAsyncioTestCase):
    async def test_native_batch_is_retired_predicted_retained_and_replayed_without_rerun(self):
        async with asyncio.timeout(150):
            await self.run_lifecycle()
