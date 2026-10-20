"""Trusted interpreter observations survive the existing two scientific input paths.

Choice, model response and retirement are recording ports here. Existing source
readers and session/planner admission are real; no external model is contacted.
"""

import json
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.integration.nmrnet.pasted_ports import PastedPorts
from tests.integration.nmrnet.smiles_ports import SessionPorts

OBSERVATION = b'{"endpoint":{"configuration_id":"fixture","model":"fixture-model"},"prompt_sha256":"authored-test-identity"}'


class InterpreterPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_supplied_and_generated_identity_retains_observation_after_chooser_retirement(self):
        async def choose(ports, route, _instructions, capability):
            if route == "pasted":
                reference = await capability.prepare_pasted(
                    start_byte=ports.start_byte, end_byte=ports.end_byte, source_format="table", source_unit="nm"
                )
            else:
                reference = await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)
            capability.submit(reference)
            return OBSERVATION

        for route in ("pasted", "smiles"):
            with self.subTest(route=route):
                if route == "pasted":
                    ports = PastedPorts(b"H 0 0 0\n", source_format="table", source_unit="nm")
                else:
                    ports = SessionPorts()
                chooser = SimpleNamespace(run=partial(choose, ports, route), retire=AsyncMock())
                preparation = ports.owner(chooser.run, chooser=chooser)
                session = ports.make_session(preparation)
                self.addAsyncCleanup(session.retire)
                handle = await session.prepare(ports.acknowledged, ports.instructions)
                self.assertEqual(ports.worker.events, [])
                chooser.retire.assert_awaited_once()
                self.assertEqual(json.loads(handle.identity.content)["interpretation"], json.loads(OBSERVATION))
                outcome = await session.execute(handle)
                self.assertEqual(json.loads(outcome.content)["interpretation"], json.loads(OBSERVATION))
                await session.retire()
                chooser.retire.assert_awaited_once()
