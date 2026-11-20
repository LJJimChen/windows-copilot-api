"""End-to-end tests for emulated tool calling over the HTTP API.

The Copilot client is faked, so these run without a signed-in account.
"""

import io
import json
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import api as server_api
from server.ratelimit import TokenBucket
from server.sessions import ConversationStore

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

SEARCH = {
    "type": "function",
    "function": {
        "name": "search",
        "description": "Web search",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    },
}

# The routing envelope the prompt asks for.
WEATHER_CALL = (
    '{"content":null,"tool_calls":'
    '[{"name":"get_weather","arguments":{"city":"Beijing"}}]}'
)
ANSWER = '{"content":"Beijing is warm today.","tool_calls":[]}'
TWO_CALLS = (
    '{"content":null,"tool_calls":['
    '{"name":"get_weather","arguments":{"city":"Oslo"}},'
    '{"name":"search","arguments":{"q":"oslo"}}]}'
)
UNKNOWN_CALL = '{"content":null,"tool_calls":[{"name":"rm_rf","arguments":{}}]}'


class FakeStream:
    """Mimics ``copilot.ChatStream``: iterable text, id known after iteration."""

    def __init__(self, text, conversation_id=None, step=7):
        self._text = text
        self._step = step
        self.conversation_id = conversation_id

    def __iter__(self):
        for i in range(0, len(self._text), self._step):
            yield self._text[i:i + self._step]


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.chat_calls = []
        self.stream_calls = []
        self._turns = 0

    def _reply(self):
        self._turns += 1
        if self.replies:
            return self.replies.pop(0)
        return "ok"

    def chat(self, prompt, conversation_id=None, **kwargs):
        self.chat_calls.append((prompt, conversation_id))
        return SimpleNamespace(
            text=self._reply(),
            conversation_id=conversation_id or f"conv-{self._turns}",
            images=[],
        )

    def stream(self, prompt, conversation_id=None, **kwargs):
        self.stream_calls.append((prompt, conversation_id))
        return FakeStream(self._reply(), conversation_id or f"conv-{self._turns}")


def sse_events(body):
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            if payload.strip() != "[DONE]":
                events.append(json.loads(payload))
    return events


class ToolCallApiTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        for target, value in (
            ("client", self.fake),
            ("_rate_limiter", TokenBucket(0, 1)),
            ("_sessions", ConversationStore()),
        ):
            patcher = patch.object(server_api, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.http = TestClient(server_api.app)

    def post(self, messages, **extra):
        payload = {"messages": messages, "tools": [WEATHER, SEARCH]}
        payload.update(extra)
        return self.http.post("/v1/chat/completions", json=payload)

    def test_non_stream_returns_tool_calls(self):
        self.fake.replies = [WEATHER_CALL]
        response = self.post([{"role": "user", "content": "weather in Beijing?"}])

        self.assertEqual(response.status_code, 200)
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertTrue(call["id"].startswith("call_"))
        self.assertEqual(call["function"]["name"], "get_weather")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "Beijing"})

    def test_fenced_json_envelope_is_accepted(self):
        self.fake.replies = ["```json\n" + WEATHER_CALL + "\n```"]
        response = self.post([{"role": "user", "content": "weather in Beijing?"}])

        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "get_weather")

    def test_normal_answer_envelope(self):
        self.fake.replies = [ANSWER]
        response = self.post([{"role": "user", "content": "weather?"}])

        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["content"], "Beijing is warm today.")
        self.assertNotIn("tool_calls", choice["message"])

    def test_tool_schemas_are_injected_into_the_prompt(self):
        self.fake.replies = [ANSWER]
        self.post([{"role": "user", "content": "hi"}])
        prompt = self.fake.chat_calls[0][0]
        self.assertIn("routing decision", prompt)
        self.assertIn("get_weather", prompt)
        self.assertIn('"required"', prompt)
        self.assertIn('"city"', prompt)
        self.assertIn('"content"', prompt)

    def test_prompt_forbids_doing_the_work_itself(self):
        self.fake.replies = [ANSWER]
        self.post([{"role": "user", "content": "write index.html"}])

        prompt = self.fake.chat_calls[0][0]
        self.assertIn("NOT being asked to execute a function", prompt)
        self.assertIn("You also have NO filesystem", prompt)
        self.assertIn("sandbox", prompt)
        self.assertIn("NEVER paste a file's contents", prompt)
        self.assertIn('"tool_calls"', prompt)

    def test_tool_choice_none_disables_tools(self):
        self.fake.replies = ["plain answer"]
        self.post([{"role": "user", "content": "hi"}], tool_choice="none")
        prompt = self.fake.chat_calls[0][0]
        self.assertNotIn("routing decision", prompt)

    def test_unknown_tool_is_not_forwarded(self):
        self.fake.replies = [UNKNOWN_CALL, UNKNOWN_CALL]
        response = self.post([{"role": "user", "content": "drop everything"}])

        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertNotIn("tool_calls", choice["message"])
        self.assertIn("rm_rf", choice["message"]["content"])

    def test_invalid_envelope_retries_once(self):
        self.fake.replies = ["I would rather just answer.", WEATHER_CALL]
        response = self.post(
            [{"role": "user", "content": "weather?"}],
            tool_choice="required",
        )

        self.assertEqual(len(self.fake.chat_calls), 2)
        self.assertIn("not a valid routing decision", self.fake.chat_calls[1][0])
        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")

    def test_named_tool_choice_only_exposes_that_tool(self):
        self.fake.replies = [WEATHER_CALL]
        self.post(
            [{"role": "user", "content": "weather?"}],
            tool_choice={"type": "function", "function": {"name": "get_weather"}},
        )
        prompt = self.fake.chat_calls[0][0]
        self.assertIn("get_weather", prompt)
        self.assertNotIn('"search"', prompt)

    def test_parallel_tool_calls_can_be_disabled(self):
        self.fake.replies = [TWO_CALLS, WEATHER_CALL]
        response = self.post(
            [{"role": "user", "content": "both"}],
            parallel_tool_calls=False,
        )
        self.assertEqual(len(self.fake.chat_calls), 2)  # protocol retry
        calls = response.json()["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")

    def test_prose_is_retried_then_accepted(self):
        self.fake.replies = ["Beijing is warm.", ANSWER]
        response = self.post([{"role": "user", "content": "weather?"}])

        choice = response.json()["choices"][0]
        self.assertEqual(choice["message"]["content"], "Beijing is warm today.")
        self.assertEqual(len(self.fake.chat_calls), 2)

    def test_raw_text_when_model_never_complies(self):
        self.fake.replies = ["Beijing is warm.", "Still just prose."]
        response = self.post([{"role": "user", "content": "weather?"}])

        choice = response.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["content"], "Still just prose.")
        self.assertEqual(len(self.fake.chat_calls), 2)

    def test_prompt_can_be_logged(self):
        self.fake.replies = [ANSWER]
        stderr = io.StringIO()
        with patch.object(server_api, "LOG_PROMPT", True), redirect_stderr(stderr):
            self.post([{"role": "user", "content": "hi"}])

        logged = stderr.getvalue()
        self.assertIn("[copilot] prompt ->", logged)
        self.assertIn("chars=", logged)
        self.assertIn("get_weather", logged)
        self.assertIn("[copilot] end prompt", logged)


class StreamingToolCallTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        for target, value in (
            ("client", self.fake),
            ("_rate_limiter", TokenBucket(0, 1)),
            ("_sessions", ConversationStore()),
        ):
            patcher = patch.object(server_api, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.http = TestClient(server_api.app)

    def stream(self, *replies, tools=True, **extra):
        self.fake.replies = list(replies)
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        if tools:
            payload["tools"] = [WEATHER]
        payload.update(extra)
        with self.http.stream("POST", "/v1/chat/completions", json=payload) as response:
            self.assertEqual(response.status_code, 200)
            return sse_events("".join(response.iter_text()))

    def content_of(self, events):
        return "".join(
            choice["delta"].get("content", "")
            for event in events for choice in event["choices"]
            if choice.get("delta")
        )

    def tool_deltas(self, events):
        return [
            part
            for event in events for choice in event["choices"]
            for part in (choice["delta"].get("tool_calls") or [])
        ]

    def finish_of(self, events):
        return [
            c["finish_reason"] for e in events for c in e["choices"] if c["finish_reason"]
        ][-1]

    def test_streams_tool_calls_and_finish_reason(self):
        events = self.stream(WEATHER_CALL)

        self.assertNotIn("tool_calls", self.content_of(events))  # raw JSON must not leak
        deltas = self.tool_deltas(events)
        self.assertEqual(deltas[0]["index"], 0)
        self.assertEqual(deltas[0]["function"]["name"], "get_weather")
        self.assertTrue(deltas[0]["id"].startswith("call_"))
        self.assertEqual(json.loads(deltas[1]["function"]["arguments"]), {"city": "Beijing"})
        self.assertEqual(self.finish_of(events), "tool_calls")

    def test_streams_normal_answer_envelope(self):
        events = self.stream(ANSWER)
        self.assertEqual(self.content_of(events), "Beijing is warm today.")
        self.assertEqual(self.tool_deltas(events), [])
        self.assertEqual(self.finish_of(events), "stop")

    def test_streams_without_tools_unchanged(self):
        text = "A perfectly normal, fairly long answer that arrives in many pieces."
        events = self.stream(text, tools=False)
        self.assertEqual(self.content_of(events), text)

    def test_required_tool_choice_buffers_and_retries(self):
        events = self.stream("prose only", WEATHER_CALL, tool_choice="required")

        self.assertEqual(len(self.fake.stream_calls), 1)
        self.assertEqual(len(self.fake.chat_calls), 1)  # the retry
        self.assertEqual(self.finish_of(events), "tool_calls")

    def test_streamed_conversation_is_reused_for_the_tool_result_turn(self):
        events = self.stream(WEATHER_CALL)
        final = [e for e in events if e["choices"][0]["finish_reason"]][-1]
        conversation_id = final["conversation_id"]

        self.fake.replies = [ANSWER]
        second = self.http.post("/v1/chat/completions", json={
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1", "type": "function",
                        "function": {"name": "get_weather",
                                     "arguments": '{"city": "Beijing"}'},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "3C, clear"},
            ],
            "tools": [WEATHER],
        })

        self.assertEqual(second.status_code, 200)
        prompt, reused_id = self.fake.chat_calls[0]
        self.assertEqual(reused_id, conversation_id)
        self.assertIn("Tool result", prompt)
        self.assertIn("3C, clear", prompt)


if __name__ == "__main__":
    unittest.main()
