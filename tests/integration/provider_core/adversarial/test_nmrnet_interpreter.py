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
    async def test_invalid_shift_precision_is_corrected_before_any_preparation_effect(self):
        for digits in (0, 18, True, None, 4.0):
            with self.subTest(digits=digits):
                ports = SessionPorts()

                # silent-defaults: allow-contract -- Bind this finite subcase's ports and invalid value for the asynchronous local peer.
                async def respond(_headers, raw, *, ports=ports, digits=digits):
                    messages = json.loads(raw)["messages"]
                    if messages[-1]["role"] == "tool":
                        self.assertIn("shift_significant_digits", json.loads(messages[-1]["content"])["rejected"])
                        ports.allocate_reader.assert_not_awaited()
                        ports.allocate_generator.assert_not_awaited()
                        ports.uploads.acquire.assert_not_awaited()
                        return tool_reply(
                            "corrected", "prepare_smiles", {"smiles": "CCl", "requested_count": 2, "shift_significant_digits": 3}
                        )
                    return tool_reply(
                        "invalid", "prepare_smiles", {"smiles": "CCl", "requested_count": 2, "shift_significant_digits": digits}
                    )

                async with TlsPeer("success", response_factory=respond) as peer:
                    chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
                    session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
                    self.addAsyncCleanup(session.retire)
                    handle = await session.prepare(ports.acknowledged, ports.instructions)
                    identity = json.loads(handle.identity.content)
                    self.assertEqual(identity["output"], {"shift_significant_digits": 3})
                    self.assertEqual(identity["interpretation"]["corrections"], 1)
                    self.assertEqual(identity["interpretation"]["turns_started"], 2)
                    ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
                    self.assertEqual(ports.worker.events, [])

    async def test_unknown_report_issue_invites_correction_without_echoing_untrusted_text(self):
        ports = SessionPorts()

        async def respond(_headers, raw):
            messages = json.loads(raw)["messages"]
            if messages[-1]["role"] == "tool":
                self.assertEqual(
                    json.loads(messages[-1]["content"]),
                    {"rejected": "The issue field must be one of the advertised choices."},
                )
                self.assertNotIn("PRIVATE_UNKNOWN_ISSUE", json.dumps(messages))
                self.assertNotIn("PRIVATE_DISCARDED_REPORT", json.dumps(messages))
                return tool_reply(
                    "corrected",
                    "report_input_problem",
                    {
                        "issue": "source_unit_unclear",
                        "message": "PRIVATE_RETAINED_REPORT",
                    },
                )
            return tool_reply(
                "invalid",
                "report_input_problem",
                {
                    "issue": "PRIVATE_UNKNOWN_ISSUE",
                    "message": "PRIVATE_DISCARDED_REPORT",
                },
            )

        async with TlsPeer("success", response_factory=respond) as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            outcome = await session.prepare(ports.acknowledged, ports.instructions)
            self.assertIsInstance(outcome, AnalysisFailure)
            self.assertIn("Specify angstrom, nm or bohr", outcome.message)
            self.assertIn("not independently verified", outcome.message)
            self.assertNotIn("PRIVATE_", outcome.message)
            report = json.loads(outcome.evidence.content)["model_report"]
            self.assertEqual(report["issue"], "source_unit_unclear")
            self.assertEqual(report["explanation"], "PRIVATE_RETAINED_REPORT")
            self.assertEqual(report["interpretation"]["corrections"], 1)
            self.assertEqual(len(peer.request_bodies), 2)
            ports.allocate_reader.assert_not_awaited()
            ports.allocate_generator.assert_not_awaited()
            self.assertEqual(ports.worker.events, [])

    async def test_real_rejection_is_corrected_in_the_same_conversation_before_one_generation(self):
        ports = SessionPorts()

        async def respond(_headers, raw):
            messages = json.loads(raw)["messages"]
            if messages[-1]["role"] == "tool":
                self.assertIn("count", json.loads(messages[-1]["content"])["rejected"])
                self.assertEqual(messages[-2]["reasoning_content"], "fixture reasoning")
                return tool_reply("corrected", "prepare_smiles", {"smiles": "CCl", "requested_count": 2})
            return tool_reply("invalid", "prepare_smiles", {"smiles": "CCl", "requested_count": 0})

        async with TlsPeer("success", response_factory=respond) as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
            session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
            self.addAsyncCleanup(session.retire)
            handle = await session.prepare(ports.acknowledged, ports.instructions)
            ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
            observation = json.loads(handle.identity.content)["interpretation"]
            self.assertEqual(observation["corrections"], 1)
            self.assertEqual(observation["turns_started"], 2)
            self.assertEqual(len(peer.request_bodies), 2)
            self.assertEqual(ports.worker.events, [])

    async def test_model_report_is_attributed_publishable_and_private_without_fallback(
        self,
    ):
        ports = SessionPorts()
        report = "PRIVATE_SOURCE_EXCERPT " + "x" * 1500

        async def respond(_headers, _raw):
            return tool_reply("report", "report_input_problem", {"issue": "other_input_problem", "message": report})

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
                    {"issue": "record_selection_unclear", "message": "The selected records could not be read."},
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
                {"smiles": "CCl", "requested_count": 2},
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
