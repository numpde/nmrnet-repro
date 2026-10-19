"""Controlled input ports; their payloads do not qualify native generation.

The installed-core runner copies this tree as tests.integration.nmrnet, which
explains the helper's import path in the structured consumer tests.
"""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.fakes.attempt_store import admission, record
from tests.fakes.job_input import facts, response
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.test_nmrnet_session import HostOwner, ScientificWorker

from nmrnet_provider.input_preparation import InputPreparation
from nmrnet_provider.session import NMRNetSession
from nmrnet_provider.smiles import GeneratedConformer, GeneratedConformers, GenerationSettings
from nmrnet_provider.xyz import prepare_xyz
from provider_core.attempt_record import acknowledge_start
from provider_core.job_input import verify_job_input
from provider_core.uploads import VerifiedUpload


class InputPorts:
    def __init__(self):
        raw = b"SMILES CCl; generate 2 conformers."
        self.instructions = verify_job_input(response(raw), feed=facts(raw))
        self.settings = GenerationSettings(17, 200, 1)
        self.source = b"1\nAuthored oxygen, angstrom\nO 0 0 0\n"
        upload = VerifiedUpload(
            "job:example", UPLOAD_REF, len(self.source), "sha256:" + hashlib.sha256(self.source).hexdigest(), self.source
        )
        self.uploads = SimpleNamespace(job_ref="job:example", inspect=AsyncMock(), acquire=AsyncMock(return_value=upload))
        self.transports = (SimpleNamespace(retire=AsyncMock()), SimpleNamespace(retire=AsyncMock()))
        self.reader = SimpleNamespace(
            read=AsyncMock(return_value=prepare_xyz(self.source, source_unit="angstrom", selection=(1,))),
            close=Mock(),
            wait_closed=AsyncMock(),
        )
        self.reader_host = SimpleNamespace(retire=AsyncMock())
        # Synthetic CCl coordinates exercise the session/planner handoff. They
        # are authored data, not evidence of native generation or bond accuracy.
        metadata = {
            "source_sha256": hashlib.sha256(b"CCl").hexdigest(),
            "requested_count": 2,
            "source_atoms": [
                {"source_atom_index": 0, "element": "C"},
                {"source_atom_index": 1, "element": "Cl"},
            ],
            "source_bonds": [{"begin_atom_index": 0, "end_atom_index": 1, "bond_type": "SINGLE"}],
            "added_hydrogen_parents": [[2, 0], [3, 0], [4, 0]],
            "component_count": 1,
            "model_projection": "Authored session fixture only.",
            "runtime": {"python": "fixture", "rdkit": "fixture"},
            "parser": {"sanitize": True, "removeHs": False, "allowCXSMILES": False, "parseName": False},
            "hydrogen_expansion": {"explicitOnly": False, "addCoords": False},
            "embedding": {"preset": "ETKDGv3", "failure_counts": [0] * 15},
        }
        stereo = json.dumps({"origin": "generated_coordinates", "method": "authored fixture", "atoms": [], "bonds": []}).encode()
        conformers = []
        for identifier in (2, 0):
            request = json.dumps(
                {
                    "atoms": ["C", "Cl", "H", "H", "H"],
                    "coordinates": [[identifier, 0, 0], [1.78, 0, 0], [-1, 1, 0], [-1, -1, 0], [-1, 0, 1]],
                    "coordinate_unit": "angstrom",
                }
            ).encode()
            conformers.append(GeneratedConformer(identifier, request, stereo))
        self.batch = GeneratedConformers(
            tuple(conformers),
            json.dumps(metadata).encode(),
            "PRIVATE_GENERATOR_DIAGNOSTIC",
        )
        self.generator = SimpleNamespace(generate=AsyncMock(return_value=self.batch), close=Mock(), wait_closed=AsyncMock())
        self.generator_host = SimpleNamespace(retire=AsyncMock())
        self.allocate_reader = AsyncMock(return_value=(self.reader, self.reader_host))
        self.allocate_generator = AsyncMock(return_value=(self.generator, self.generator_host))

    def owner(self, script, **changes):
        configuration = {
            "uploads": self.uploads,
            "source_api_transport": self.transports[0],
            "source_store_transport": self.transports[1],
            "allocate_reader": self.allocate_reader,
            "allocate_generator": self.allocate_generator,
            "generation_settings": self.settings,
            "generation_seconds": 2,
            "chooser": SimpleNamespace(run=script),
            "max_worker_calls": 2,
            "seconds": 5,
        }
        configuration.update(changes)
        return InputPreparation(**configuration)


class SessionPorts(InputPorts):
    """One real binding and input owner, with recording generation/model ports."""

    def __init__(self):
        super().__init__()

        async def choose(_instructions, capability):
            reference = await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)
            capability.submit(reference)

        member = {"archive_sha256": "a" * 64, "member": "fixture", "size": 1, "sha256": "b" * 64}
        artifacts = {
            "dictionary": member,
            "elements": {head: {"checkpoint": member, "scaler": member} for head in ("H", "C")},
        }
        runtime = {"python": "3.12.12", "installed_distributions": {"scikit-learn": "1.8.0"}}
        self.worker = ScientificWorker(artifacts, runtime)
        self.host = HostOwner()
        self.preparation = self.owner(choose)
        self.pending = replace(record(), input=self.instructions.identity)
        self.acknowledged = acknowledge_start(self.pending, admission())
        self.session = self.make_session(self.preparation)

    def make_session(self, preparation):
        return NMRNetSession(
            worker=self.worker,
            retirement=self.host,
            execution_identity={"image_id": "fixture"},
            selected_artifacts=self.worker.artifacts,
            runtime_expectations=self.worker.runtime,
            input_preparation=preparation,
        )
