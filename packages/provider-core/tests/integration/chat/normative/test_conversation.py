"""One shared transcript carries completed tool effects across model endpoints."""

import json
import unittest

from provider_core.chat import ChatFailed
from provider_core.conversation import ToolConversation, ToolFinished, ToolReply
from tests.fakes.conversation import call, endpoint


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_tool_and_reasoning_survive_fallback_without_repeating_effect(
        self,
    ):
        first = endpoint(
            "first",
            call("prepare-1", "prepare", {}),
            ChatFailed("exchange_failed", "Fixture reply lost."),
        )
        second = endpoint("second", call("submit-1", "submit", {"reference": "owned-reference"}))
        effects = []

        async def dispatch(name, arguments):
            effects.append(name)
            if name == "prepare":
                return ToolReply({"reference": "owned-reference"})
            first.retire.assert_awaited_once()
            self.assertEqual(arguments, {"reference": "owned-reference"})
            return ToolFinished("submitted")

        conversation = ToolConversation((first, second), max_turns=3, secrets=())
        result = await conversation.run([{"role": "user", "content": "fixture request"}], [], dispatch)
        self.assertEqual(result.value, "submitted")
        self.assertEqual(effects, ["prepare", "submit"])
        transcript = second.complete.call_args.args[0]
        self.assertEqual(transcript[1]["reasoning_content"], "fixture reasoning")
        self.assertEqual(json.loads(transcript[2]["content"]), {"reference": "owned-reference"})
        observed = json.loads(result.observation)
        self.assertEqual(observed["turns_started"], 3)
        self.assertEqual(
            [route["endpoint"]["configuration_id"] for route in observed["routes"]],
            ["first", "second"],
        )
        self.assertEqual(observed["routes"][0]["failure"], "exchange_failed")
        self.assertNotIn(b"owned-reference", result.observation)
        await conversation.retire()
