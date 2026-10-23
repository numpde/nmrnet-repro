"""Correction, terminal reports and operational failures keep their own authority."""

import asyncio
import json
import unittest
from unittest.mock import patch

import httpcore
from tests.fakes.attempts import ATTEMPT_REF
from tests.fakes.controller import cancel_task
from tests.fakes.execution import unexpected_condition
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.interpreter_ports import chat_endpoint, tool_reply
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.interpreter import InputInterpreter
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.session import PreparationEvidenceFailed
from provider_core.analysis import AnalysisFailure
from provider_core.attempt_request import fail_body
from provider_core.conversation import ConversationFailed


class InterpreterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_model_post_retains_final_private_diagnostics_after_received_headers(self):
        await self.check_interrupted_post(repeated_cancellation=False)

    async def test_late_shared_retirement_retains_diagnostics_after_preparation_waiter_is_cancelled_twice(self):
        await self.check_interrupted_post(repeated_cancellation=True)

    async def check_interrupted_post(self, *, repeated_cancellation):
        ports = SessionPorts()
        received_head = asyncio.Event()
        original = httpcore.AsyncConnectionPool.handle_async_request

        async def observe_head(pool, request):
            response = await original(pool, request)
            received_head.set()
            return response

        async with TlsPeer(
            "body-stall",
            response_headers=((b"x-request-id", b"request:input-selection"), (b"x-private", b"PRIVATE_HEADER")),
        ) as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
            owner = ports.owner(chooser.run, chooser=chooser)
            retiring, release_retirement = asyncio.Event(), asyncio.Event()
            original_retire = chooser._conversation.retire

            async def held_retirement():
                retiring.set()
                await release_retirement.wait()
                await original_retire()

            if repeated_cancellation:
                chooser._conversation.retire = held_retirement
            task = None
            try:
                with patch.object(httpcore.AsyncConnectionPool, "handle_async_request", new=observe_head):
                    task = asyncio.create_task(owner.prepare(ports.instructions))
                    await asyncio.wait_for(received_head.wait(), 1)
                    task.cancel()
                    if repeated_cancellation:
                        await asyncio.wait_for(retiring.wait(), 1)
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    if repeated_cancellation:
                        # Only the outer waiter ended. The original shared
                        # retirement task finishes while preparation is FINISHED.
                        release_retirement.set()
                        await asyncio.wait_for(asyncio.shield(owner._chooser_retirement_task), 1)
                diagnostics = json.loads(owner.interpreter_diagnostics)
                rendered = json.dumps(diagnostics)
                for fact in ("request:input-selection", '"status": 200', '"reason": "request_cancelled"'):
                    self.assertIn(fact, rendered)
                for secret in ("PRIVATE_MODEL_KEY", "PRIVATE_HEADER", ports.instructions.specification_text):
                    self.assertNotIn(secret, rendered)
                # Evidence-capacity failures use the same existing entry shape
                # when projecting the final model cause to the operator.
                limited = PreparationEvidenceFailed({"interpreter_diagnostics": diagnostics})
                summary = json.dumps(failure_evidence(limited, secrets=("PRIVATE_MODEL_KEY",)))
                self.assertIn("request:input-selection", summary)
                self.assertIn("request_cancelled", summary)
                self.assertEqual(len(peer.requests), 1)
                ports.allocate_reader.assert_not_awaited()
                ports.allocate_generator.assert_not_awaited()
            finally:
                release_retirement.set()
                if task is not None:
                    await cancel_task(task)
                await owner.retire()

    async def test_failed_interpreter_cannot_retarget_old_diagnostics_to_another_capability(self):
        ports, other_ports = SessionPorts(), SessionPorts()
        async with TlsPeer("success", response_status=503, response_body=b"PRIVATE_BODY") as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
            owner = ports.owner(chooser.run, chooser=chooser)
            other = other_ports.owner(chooser.run, chooser=chooser)
            try:
                with self.assertRaises(ConversationFailed):
                    await owner.prepare(ports.instructions)
                self.assertIsNotNone(owner.interpreter_diagnostics)
                with self.assertRaisesRegex(ValueError, "diagnostic recipient"):
                    await chooser.run(other_ports.instructions, other)
                await owner.retire()
                self.assertIsNone(other.interpreter_diagnostics)
                self.assertEqual(len(peer.requests), 1)
            finally:
                await owner.retire()

    async def test_corrected_reader_rejection_keeps_private_diagnostics_and_prior_endpoint_failure_before_generation(self):
        ports = SessionPorts()
        rejection = InputRejected(
            "Cannot select those records.",
            diagnostic="PRIVATE reader dump PRIVATE_MODEL_KEY api_key=PRIVATE_CREDENTIAL " + "x" * 12000 + " END_READER_DUMP",
        )
        ports.reader.read.side_effect = rejection

        async def generate(_smiles, _count):
            diagnostics = json.loads(owner.interpreter_diagnostics)
            self.assertEqual([entry.get("turn_index") for entry in diagnostics], [None, 2])
            self.assertEqual(diagnostics[0]["endpoint_index"], 0)
            self.assertIn(str(rejection), json.dumps(diagnostics))
            return ports.batch

        ports.generator.generate.side_effect = generate

        async def lost_reply(_headers, _raw):
            return None

        async def respond(_headers, raw):
            messages = json.loads(raw)["messages"]
            if messages[-1]["role"] == "tool":
                self.assertEqual(json.loads(messages[-1]["content"]), {"rejected": str(rejection)})
                self.assertNotIn("PRIVATE reader dump", json.dumps(messages))
                return tool_reply("corrected", "prepare_smiles", {"smiles": "CCl", "requested_count": 2})
            return tool_reply(
                "rejected",
                "prepare_upload",
                {
                    "upload_ref": ports.uploads.acquire.return_value.upload_ref,
                    "source_format": "xyz",
                    "source_unit": "angstrom",
                    "selection": [1],
                    "alternate_location": None,
                },
            )

        async with (
            TlsPeer("success", response_factory=lost_reply) as first,
            TlsPeer("success", response_factory=respond) as second,
        ):
            chooser = InputInterpreter(
                (chat_endpoint(first, identifier="first"), chat_endpoint(second, identifier="second")), max_turns=3
            )
            owner = ports.owner(chooser.run, chooser=chooser)
            session = ports.make_session(owner)
            self.addAsyncCleanup(session.retire)
            handle = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
            prepared = json.loads(handle.identity.content)
            private = json.dumps(prepared["interpreter_diagnostics"])
            self.assertIn(str(rejection), private)
            self.assertIn("PRIVATE reader dump", private)
            self.assertIn("END_READER_DUMP", private)
            self.assertIn("omitted_characters", private)
            self.assertNotIn("PRIVATE_MODEL_KEY", private)
            self.assertNotIn("PRIVATE_CREDENTIAL", private)
            self.assertLess(len(private), 16000)
            self.assertEqual(prepared["interpretation"]["corrections"], 1)
            self.assertNotIn("Cannot select", json.dumps(prepared["interpretation"]))
            self.assertEqual(ports.worker.events, [])
            outcome = await session.execute(handle, report_condition=unexpected_condition)
            self.assertNotIn("interpreter_diagnostics", json.loads(outcome.content))
            self.assertNotIn(b"PRIVATE reader dump", outcome.content)
            self.assertEqual(json.loads(outcome.evidence.content)["interpreter_diagnostics"], prepared["interpreter_diagnostics"])
            ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
            ports.reader.read.assert_awaited_once()
            self.assertEqual(ports.worker.events, ["prepare", "execute"])

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
                            "corrected",
                            "prepare_smiles",
                            {"smiles": "CCl", "requested_count": 2, "output": {"shift_significant_digits": 3}},
                        )
                    return tool_reply(
                        "invalid",
                        "prepare_smiles",
                        {"smiles": "CCl", "requested_count": 2, "output": {"shift_significant_digits": digits}},
                    )

                async with TlsPeer("success", response_factory=respond) as peer:
                    chooser = InputInterpreter((chat_endpoint(peer),), max_turns=2)
                    session = ports.make_session(ports.owner(chooser.run, chooser=chooser))
                    self.addAsyncCleanup(session.retire)
                    handle = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
                    identity = json.loads(handle.identity.content)
                    self.assertEqual(identity["output"], {"shift_significant_digits": 3, "verbosity": "medium"})
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
            outcome = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
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
            handle = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
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
            outcome = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
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
            outcome = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
            self.assertEqual(outcome.code, "interpreter_reported_input_problem")
            private = json.loads(outcome.evidence.content)["interpreter_diagnostics"]
            self.assertEqual(private[0]["turn_index"], 1)
            self.assertIn(str(failure), json.dumps(private))
            self.assertIn("PRIVATE reader dump", json.dumps(private))
            self.assertNotIn("PRIVATE reader dump", outcome.message)
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
                await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
            self.assertIs(caught.exception, failure)
            self.assertEqual(len(peer.request_bodies), 1)
            ports.generator_host.retire.assert_awaited_once()
            self.assertEqual(ports.worker.events, [])
