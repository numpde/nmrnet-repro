"""Correction, terminal reports and operational failures keep their own authority."""

import json
import unittest

from tests.fakes.attempts import ATTEMPT_REF
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.interpreter_ports import chat_endpoint, tool_reply
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.interpreter import InputInterpreter
from provider_core.analysis import AnalysisFailure
from provider_core.attempt_request import fail_body


class InterpreterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_reference_is_corrected_without_repeating_generation(self):
        ports = SessionPorts()
        references = []

        async def respond(_headers, raw):
            messages = json.loads(raw)["messages"]
            tools = [m for m in messages if m["role"] == "tool"]
            if not tools:
                return tool_reply(
                    "prepare",
                    "prepare_smiles",
                    {"start_byte": 7, "end_byte": 10, "requested_count": 2},
                )
            if not references:
                references.append(json.loads(tools[0]["content"])["reference"])
                return tool_reply("stale", "submit", {"reference": "invented-reference"})
            self.assertIn("rejected", json.loads(tools[-1]["content"]))
            return tool_reply("submit", "submit", {"reference": references[0]})

        async with TlsPeer("success", response_factory=respond) as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=3)
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            handle = await session.prepare(ports.acknowledged, ports.instructions)
            ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
            self.assertEqual(json.loads(handle.identity.content)["interpretation"]["corrections"], 1)
            self.assertEqual(ports.worker.events, [])

    async def test_model_report_is_attributed_publishable_and_private_without_fallback(
        self,
    ):
        ports = SessionPorts()
        report = "PRIVATE_SOURCE_EXCERPT " + "x" * 1500

        async def respond(_headers, _raw):
            return tool_reply("report", "report_input_problem", {"message": report})

        async with TlsPeer("success", response_factory=respond) as peer:
            first = chat_endpoint(peer, identifier="first")
            unused = chat_endpoint(peer, identifier="unused")
            chooser = InputInterpreter((first, unused), max_turns=4)
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            outcome = await session.prepare(ports.acknowledged, ports.instructions)
            self.assertIsInstance(outcome, AnalysisFailure)
            self.assertEqual(outcome.code, "interpreter_reported_input_problem")
            published = fail_body(ATTEMPT_REF, outcome.code, outcome.message)
            self.assertNotIn(b"PRIVATE_SOURCE_EXCERPT", published)
            self.assertIn("not independently verified", outcome.message)
            model_report = json.loads(outcome.evidence.content)["model_report"]
            self.assertEqual(model_report["explanation"], report)
            self.assertIs(model_report["independently_verified"], False)
            self.assertEqual(
                model_report["interpretation"]["outcome"],
                "model_reported_input_problem",
            )
            self.assertEqual(len(peer.request_bodies), 1)
            self.assertEqual(ports.worker.events, [])
            ports.allocate_generator.assert_not_awaited()
            ports.uploads.acquire.assert_not_awaited()
            self.assertTrue(first.retirement_confirmed and unused.retirement_confirmed)

    async def test_expected_reader_rejection_returns_only_its_safe_reason(self):
        ports = SessionPorts()
        failure = InputRejected("Cannot select those records.", diagnostic="PRIVATE reader dump")
        ports.reader.read.side_effect = failure

        async def respond(_headers, raw):
            messages = json.loads(raw)["messages"]
            if messages[-1]["role"] == "tool":
                self.assertNotIn("PRIVATE", json.dumps(messages))
                self.assertEqual(json.loads(messages[-1]["content"]), {"rejected": str(failure)})
                return tool_reply(
                    "report",
                    "report_input_problem",
                    {"message": "The selected records could not be read."},
                )
            return tool_reply(
                "prepare",
                "prepare_upload",
                {
                    "upload_ref": ports.uploads.acquire.return_value.upload_ref,
                    "source_format": "xyz",
                    "source_unit": "angstrom",
                    "selection": [1],
                    "alternate_location": None,
                },
            )

        async with TlsPeer("success", response_factory=respond) as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            outcome = await session.prepare(ports.acknowledged, ports.instructions)
            self.assertEqual(outcome.code, "interpreter_reported_input_problem")
            ports.reader_host.retire.assert_awaited_once()
            self.assertEqual(ports.worker.events, [])

    async def test_operational_generation_failure_cannot_become_model_correction_or_fallback(
        self,
    ):
        ports = SessionPorts()
        failure = OSError("PRIVATE native worker crash")
        ports.generator.generate.side_effect = failure

        async def respond(_headers, _raw):
            return tool_reply(
                "prepare",
                "prepare_smiles",
                {"start_byte": 7, "end_byte": 10, "requested_count": 2},
            )

        async with TlsPeer("success", response_factory=respond) as peer:
            chooser = InputInterpreter(
                (
                    chat_endpoint(peer, identifier="first"),
                    chat_endpoint(peer, identifier="unused"),
                ),
                max_turns=4,
            )
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            with self.assertRaises(OSError) as caught:
                await session.prepare(ports.acknowledged, ports.instructions)
            self.assertIs(caught.exception, failure)
            self.assertEqual(len(peer.request_bodies), 1)
            ports.generator_host.retire.assert_awaited_once()
            self.assertEqual(ports.worker.events, [])
