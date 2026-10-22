"""Trusted interpreter observations survive the existing two scientific input paths.

Choice, model response and retirement are recording ports here. Existing source
readers and session/planner admission are real; no external model is contacted.
"""

import json
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.fakes.execution import unexpected_condition
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.interpreter_ports import UploadPorts
from tests.integration.nmrnet.pasted_ports import PastedPorts
from tests.integration.nmrnet.smiles_ports import SessionPorts

OBSERVATION = b'{"endpoint":{"configuration_id":"fixture","model":"fixture-model"},"prompt_sha256":"authored-test-identity"}'


class InterpreterPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_prepare_capability_freezes_its_selected_precision(self):
        for route in ("pasted", "smiles", "upload"):
            with self.subTest(route=route):
                ports = (
                    PastedPorts(b"H 0 0 0\n", source_format="table", source_unit="nm")
                    if route == "pasted"
                    else UploadPorts()
                    if route == "upload"
                    else SessionPorts()
                )

                # silent-defaults: allow-contract -- Bind this finite route and its source ports while its chooser runs asynchronously.
                async def choose(_instructions, capability, *, route=route, ports=ports):
                    if route == "pasted":
                        await capability.prepare_pasted(
                            source_text=ports.source_text, source_format="table", source_unit="nm", shift_significant_digits=5
                        )
                    elif route == "upload":
                        await capability.prepare_upload(
                            UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2, 1), shift_significant_digits=5
                        )
                    else:
                        await capability.prepare_smiles(smiles="CCl", requested_count=2, shift_significant_digits=5)

                owner = ports.owner(choose)
                self.addAsyncCleanup(owner.retire)
                prepared = await owner.prepare(ports.instructions)
                self.assertEqual(prepared.shift_significant_digits, 5)

    async def test_supplied_and_generated_identity_retains_observation_after_chooser_retirement(self):
        async def choose(ports, route, _instructions, capability):
            if route == "pasted":
                await capability.prepare_pasted(source_text=ports.source_text, source_format="table", source_unit="nm")
            else:
                await capability.prepare_smiles(smiles="CCl", requested_count=2)

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
                handle = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
                self.assertEqual(ports.worker.events, [])
                chooser.retire.assert_awaited_once()
                self.assertEqual(json.loads(handle.identity.content)["interpretation"], json.loads(OBSERVATION))
                outcome = await session.execute(handle, report_condition=unexpected_condition)
                self.assertEqual(json.loads(outcome.content)["interpretation"], json.loads(OBSERVATION))
                await session.retire()
                chooser.retire.assert_awaited_once()
