"""Controlled input ports; their payloads do not qualify native generation.

The installed-core runner copies this tree as tests.integration.nmrnet, which
explains the helper's import path in the structured consumer tests.
"""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.fakes.job_input import facts, response
from tests.fakes.uploads import UPLOAD_REF

from nmrnet_provider.input_preparation import InputPreparation
from nmrnet_provider.smiles import GeneratedConformer, GeneratedConformers, GenerationSettings
from nmrnet_provider.xyz import prepare_xyz
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
        # The owner must preserve an admitted port result, not inspect chemistry.
        # Protocol and real-producer tests separately qualify the payload schema.
        request = b'{"atoms":["C"],"coordinates":[[0,0,0]],"coordinate_unit":"angstrom"}'
        self.batch = GeneratedConformers(
            tuple(GeneratedConformer(identifier, request, b"{}") for identifier in (2, 0)),
            json.dumps({"source_sha256": hashlib.sha256(b"CCl").hexdigest(), "requested_count": 2}).encode(),
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
