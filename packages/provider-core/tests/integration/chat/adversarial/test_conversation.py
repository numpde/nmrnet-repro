"""Rejected model turns consume the same budget and cannot replay tool effects."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from provider_core.chat import ChatFailed
from provider_core.conversation import (
    ConversationFailed,
    ConversationRetirementFailed,
    ToolConversation,
    ToolFinished,
    ToolReply,
)
from tests.fakes.conversation import call, endpoint


class ConversationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_arguments_and_multiple_calls_are_corrected_without_dispatch(
        self,
    ):
        for text in (
            '{"x":1,"x":2}',
            '{"x":NaN}',
            '{"x":1e999}',
            "[]",
            "PRIVATE malformed json",
        ):
            with self.subTest(arguments=text):
                malformed = call("bad", "submit", {})
                malformed["tool_calls"][0]["function"]["arguments"] = text
                multiple = call("multi-1", "submit", {})
                multiple["tool_calls"].extend(call("multi-2", "submit", {})["tool_calls"])
                model = endpoint("fixture", malformed, multiple, call("good", "submit", {}))
                dispatch = AsyncMock(return_value=ToolFinished("accepted"))
                conversation = ToolConversation((model,), max_turns=3, secrets=())
                result = await conversation.run([], [], dispatch)
                dispatch.assert_awaited_once_with("submit", {})
                self.assertEqual(json.loads(result.observation)["corrections"], 2)
                feedback = [m for m in model.complete.call_args.args[0] if m["role"] == "tool"]
                self.assertEqual([m["tool_call_id"] for m in feedback], ["bad", "multi-1", "multi-2"])
                self.assertNotIn("PRIVATE", json.dumps(feedback))
                await conversation.retire()

    async def test_duplicate_ids_and_exhaustion_do_not_repeat_a_completed_tool(self):
        model = endpoint("fixture", call("same", "prepare", {}), call("same", "prepare", {}))
        dispatch = AsyncMock(return_value=ToolReply({"reference": "retained"}))
        conversation = ToolConversation((model,), max_turns=2, secrets=())
        with self.assertRaises(ConversationFailed):
            await conversation.run([], [], dispatch)
        dispatch.assert_awaited_once()
        await conversation.retire()

    async def test_tool_failure_is_operational_without_feedback_or_fallback(self):
        failure = OSError("PRIVATE reader crash")
        first = endpoint("first", call("prepare", "prepare", {}))
        second = endpoint("second", call("other", "prepare", {}))
        conversation = ToolConversation((first, second), max_turns=4, secrets=())
        with self.assertRaises(OSError) as caught:
            await conversation.run([], [], AsyncMock(side_effect=failure))
        self.assertIs(caught.exception, failure)
        second.complete.assert_not_awaited()
        await conversation.retire()

    async def test_uncertain_retirement_blocks_endpoint_switch_and_keeps_both_causes(
        self,
    ):
        primary = ChatFailed("exchange_failed", "Fixture reply lost.")
        cleanup = OSError("PRIVATE socket cleanup")
        first, second = (
            endpoint("first", primary),
            endpoint("second", call("unused", "submit", {})),
        )
        first.retire.side_effect = cleanup
        conversation = ToolConversation((first, second), max_turns=3, secrets=())
        with self.assertRaises(ConversationRetirementFailed) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertIs(caught.exception.cause, primary)
        self.assertIs(caught.exception.failures[0][1], cleanup)
        second.complete.assert_not_awaited()
        first.retire.side_effect = None
        await conversation.retire()

    async def test_cancellation_never_starts_another_endpoint(self):
        primary = asyncio.CancelledError()
        first, second = (
            endpoint("first", primary),
            endpoint("second", call("unused", "submit", {})),
        )
        conversation = ToolConversation((first, second), max_turns=3, secrets=())
        with self.assertRaises(asyncio.CancelledError) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertIs(caught.exception, primary)
        second.complete.assert_not_awaited()
        await conversation.retire()

    async def test_late_reply_cannot_dispatch_after_retirement(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def late_reply(*_):
            entered.set()
            await release.wait()
            return call("late", "submit", {})

        model = endpoint("fixture")
        model.complete.side_effect = late_reply
        dispatch = AsyncMock(return_value=ToolFinished("too late"))
        conversation = ToolConversation((model,), max_turns=1, secrets=())
        running = asyncio.create_task(conversation.run([], [], dispatch))
        await entered.wait()
        await conversation.retire()
        release.set()
        with self.assertRaisesRegex(ConversationFailed, "retired"):
            await running
        dispatch.assert_not_awaited()

    async def test_later_cleanup_keeps_the_original_tool_failure(self):
        primary, cleanup = OSError("PRIVATE reader"), OSError("PRIVATE cleanup")
        first, second = (
            endpoint("first", call("prepare", "prepare", {})),
            endpoint("second"),
        )
        conversation = ToolConversation((first, second), max_turns=2, secrets=())
        with self.assertRaises(OSError):
            await conversation.run([], [], AsyncMock(side_effect=primary))
        first.retire.side_effect = cleanup
        with self.assertRaises(ConversationRetirementFailed) as caught:
            await conversation.retire()
        self.assertIs(caught.exception.cause, primary)
        self.assertIs(caught.exception.failures[0][1], cleanup)
        second.retire.assert_awaited_once()

    async def test_only_admitted_carrier_fields_enter_following_requests(self):
        message = call("prepare", "prepare", {})
        message["PRIVATE_unknown"] = "PRIVATE value"
        message["tool_calls"][0]["function"]["PRIVATE_extra"] = "PRIVATE value"
        model = endpoint("fixture", message, call("submit", "submit", {}))
        dispatch = AsyncMock(side_effect=(ToolReply({"reference": "owned"}), ToolFinished(None)))
        conversation = ToolConversation((model,), max_turns=2, secrets=())
        await conversation.run([], [], dispatch)
        self.assertNotIn("PRIVATE", json.dumps(model.complete.call_args.args))
        await conversation.retire()

    async def test_secret_values_are_checked_without_rewriting_json_structure(self):
        for secret in ('"', "\\", "model"):
            with self.subTest(secret=secret):
                model = endpoint("fixture", call("submit", "submit", {}))
                conversation = ToolConversation((model,), max_turns=1, secrets=(secret,))
                result = await conversation.run(
                    [{"role": "user", "content": "safe request"}],
                    [],
                    AsyncMock(return_value=ToolFinished(None)),
                )
                identity = json.loads(result.observation)["routes"][0]["endpoint"]
                self.assertEqual(identity["model"], "fixture")
                await conversation.retire()

    async def test_credentials_in_tool_declarations_never_reach_the_endpoint(self):
        model = endpoint("fixture")
        conversation = ToolConversation((model,), max_turns=1, secrets=("PRIVATE key",))
        with self.assertRaisesRegex(ConversationFailed, "configured credential"):
            await conversation.run([], [{"description": "PRIVATE key"}], AsyncMock())
        model.complete.assert_not_awaited()
        await conversation.retire()

    async def test_decoded_argument_credentials_cannot_reach_another_turn_or_endpoint(self):
        for raw in ('{"message":"\\u0050RIVATE_MODEL_KEY"}', '{"\\u0050RIVATE_MODEL_KEY":0}'):
            with self.subTest(arguments=raw):
                message = call("report", "report", {})
                message["tool_calls"][0]["function"]["arguments"] = raw
                first, second = endpoint("first", message), endpoint("second", call("unused", "submit", {}))
                conversation = ToolConversation((first, second), max_turns=3, secrets=("PRIVATE_MODEL_KEY",))
                dispatch = AsyncMock()
                with self.assertRaisesRegex(ConversationFailed, "configured credential"):
                    await conversation.run([], [], dispatch)
                self.assertEqual(first.complete.await_count, 1)
                second.complete.assert_not_awaited()
                dispatch.assert_not_awaited()
                await conversation.retire()

    async def test_fallback_preserves_each_private_failure_without_publishing_its_body(self):
        failures = (
            ChatFailed("exchange_failed", "First reply lost.", cause=OSError("PRIVATE first")),
            ChatFailed("exchange_failed", "Second reply lost.", cause=OSError("PRIVATE second")),
        )
        models = (
            endpoint("first", failures[0]),
            endpoint("second", failures[1]),
            endpoint("third", call("submit", "submit", {})),
        )
        conversation = ToolConversation(models, max_turns=3, secrets=())
        result = await conversation.run([], [], AsyncMock(return_value=ToolFinished(None)))
        await conversation.retire()
        self.assertEqual(conversation.endpoint_failures, [(0, failures[0]), (1, failures[1])])
        self.assertNotIn(b"PRIVATE", result.observation)

    async def test_failed_exchange_uses_the_shared_turn_budget_before_fallback(self):
        failure = ChatFailed("exchange_failed", "Fixture reply lost.")
        first, second = endpoint("first", failure), endpoint("second", call("unused", "submit", {}))
        conversation = ToolConversation((first, second), max_turns=1, secrets=())
        with self.assertRaises(ConversationFailed) as caught:
            await conversation.run([], [], AsyncMock())
        self.assertIs(caught.exception.cause, failure)
        second.complete.assert_not_awaited()
        first.retire.assert_awaited_once()
        await conversation.retire()
