"""Trusted interpreter observations survive the existing two scientific input paths.

Choice, model response and retirement are recording ports here. Existing source
readers and session/planner admission are real; no external model is contacted.
"""

import asyncio
import json
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.fakes.execution import unexpected_condition
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.interpreter_ports import UploadPorts, chat_endpoint
from tests.integration.nmrnet.pasted_ports import PastedPorts
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.interpreter import InputInterpreter
from provider_core.analysis import AnalysisFailure

OBSERVATION = b'{"endpoint":{"configuration_id":"fixture","model":"fixture-model"},"prompt_sha256":"authored-test-identity"}'


class InterpreterPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_input_deadline_during_model_post_returns_job_failure_with_final_diagnostics(self):
        ports = SessionPorts()
        async with TlsPeer("body-stall", response_headers=((b"x-request-id", b"request:input-time-limit"),)) as peer:
            chooser = InputInterpreter((chat_endpoint(peer, exchange_seconds=2),), max_turns=2)
            owner = ports.owner(chooser.run, chooser=chooser, seconds=0.5)
            session = ports.make_session(owner)
            self.addAsyncCleanup(session.retire)
            outcome = await asyncio.wait_for(
                session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition),
                2,
            )
            self.assertIsInstance(outcome, AnalysisFailure)
            self.assertEqual(outcome.code, "input_selection_time_limit")
            self.assertIn("0.5", outcome.message)
            self.assertIn("operator", outcome.message)
            evidence = json.loads(outcome.evidence.content)
            self.assertEqual(evidence["input_selection_time_limit"], {"seconds": 0.5})
            diagnostics = json.dumps(evidence["interpreter_diagnostics"])
            for fact in ("request:input-time-limit", "request_cancelled", "may have been processed"):
                self.assertIn(fact, diagnostics)
            self.assertNotIn("PRIVATE_MODEL_KEY", diagnostics)
            self.assertEqual(len(peer.requests), 1)
            ports.allocate_reader.assert_not_awaited()
            ports.allocate_generator.assert_not_awaited()
            self.assertEqual(ports.worker.events, [])
            for transport in ports.transports:
                transport.retire.assert_awaited_once()

    async def test_owned_model_wait_deadline_after_fallback_keeps_both_endpoint_causes(self):
        ports = SessionPorts()
        async with (
            TlsPeer("success", response_status=503, response_body=b"PRIVATE_MODEL_BODY") as unavailable,
            TlsPeer("body-stall", response_headers=((b"x-request-id", b"request:after-fallback"),)) as waiting,
        ):
            chooser = InputInterpreter(
                (chat_endpoint(unavailable, identifier="first"), chat_endpoint(waiting, identifier="second", exchange_seconds=2)),
                max_turns=2,
            )
            owner = ports.owner(chooser.run, chooser=chooser, seconds=0.5)
            session = ports.make_session(owner)
            self.addAsyncCleanup(session.retire)
            outcome = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
            self.assertIsInstance(outcome, AnalysisFailure)
            self.assertEqual(outcome.code, "input_selection_time_limit")
            diagnostics = json.dumps(json.loads(outcome.evidence.content)["interpreter_diagnostics"])
            self.assertIn('"status": 503', diagnostics)
            self.assertIn("request:after-fallback", diagnostics)
            self.assertIn("request_cancelled", diagnostics)
            self.assertNotIn("PRIVATE_MODEL_KEY", diagnostics)
            self.assertNotIn("PRIVATE_MODEL_BODY", diagnostics)
            self.assertEqual((len(unavailable.requests), len(waiting.requests)), (1, 1))
            self.assertEqual(ports.worker.events, [])

    async def test_each_prepare_capability_freezes_the_shared_output_fragment(self):
        for route in ("pasted", "smiles", "upload"):
            with self.subTest(route=route):
                ports = (
                    PastedPorts(b"H 0 0 0\n", source_format="table", source_unit="nm")
                    if route == "pasted"
                    else UploadPorts()
                    if route == "upload"
                    else SessionPorts()
                )
                output = {
                    "verbosity": {"pasted": "low", "upload": "medium", "smiles": "max"}[route],
                    "shift_significant_digits": 5,
                }

                # silent-defaults: allow-contract -- Bind this finite route and its source ports while its chooser runs asynchronously.
                async def choose(_instructions, capability, *, route=route, ports=ports, output=output):
                    if route == "pasted":
                        await capability.prepare_pasted(
                            source_text=ports.source_text, source_format="table", source_unit="nm", output=output
                        )
                    elif route == "upload":
                        await capability.prepare_upload(
                            UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2, 1), output=output
                        )
                    else:
                        await capability.prepare_smiles(smiles="CCl", requested_count=2, output=output)

                owner = ports.owner(choose)
                self.addAsyncCleanup(owner.retire)
                prepared = await owner.prepare(ports.instructions)
                self.assertEqual(prepared.output.as_dict(), output)
                output["verbosity"] = "untrusted later mutation"
                self.assertNotEqual(prepared.output.verbosity.value, output["verbosity"])

    async def test_supplied_and_generated_identity_retains_observation_after_chooser_retirement(self):
        async def choose(ports, route, _instructions, capability):
            if route == "pasted":
                await capability.prepare_pasted(
                    source_text=ports.source_text, source_format="table", source_unit="nm", output={"verbosity": "max"}
                )
            else:
                await capability.prepare_smiles(smiles="CCl", requested_count=2, output={"verbosity": "max"})

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
