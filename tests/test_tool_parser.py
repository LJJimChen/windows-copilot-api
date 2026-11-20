"""Tests for the strict routing-envelope parser.

The parser is the only way a text reply can become an OpenAI ``tool_calls``
array, so these tests focus on rejecting everything that is not exactly the
requested envelope.
"""

import json
import unittest

from server.tool_parser import ToolOutputError, parse_tool_output

ALLOWED = {"get_weather", "search"}


def parse(text, allowed=ALLOWED, **kw):
    return parse_tool_output(text, allowed=allowed, **kw)


def envelope(content=None, calls=None):
    return json.dumps({"content": content, "tool_calls": calls or []})


class CallTests(unittest.TestCase):
    def assert_call(self, reply, name, arguments):
        self.assertEqual([c["function"]["name"] for c in reply.tool_calls], [name])
        self.assertEqual(json.loads(reply.tool_calls[0]["function"]["arguments"]), arguments)
        self.assertTrue(reply.tool_calls[0]["id"].startswith("call_"))
        self.assertEqual(reply.tool_calls[0]["type"], "function")

    def test_valid_call_envelope(self):
        reply = parse(envelope(calls=[{"name": "get_weather", "arguments": {"city": "Oslo"}}]))
        self.assertIsNone(reply.content)
        self.assert_call(reply, "get_weather", {"city": "Oslo"})

    def test_fenced_json_is_tolerated(self):
        raw = "```json\n" + envelope(
            calls=[{"name": "get_weather", "arguments": {"city": "Oslo"}}]
        ) + "\n```"
        self.assert_call(parse(raw), "get_weather", {"city": "Oslo"})

    def test_empty_arguments_object(self):
        reply = parse(envelope(calls=[{"name": "search", "arguments": {}}]))
        self.assert_call(reply, "search", {})

    def test_parallel_calls(self):
        reply = parse(envelope(calls=[
            {"name": "get_weather", "arguments": {"city": "Oslo"}},
            {"name": "search", "arguments": {"q": "oslo"}},
        ]))
        self.assertEqual(
            [c["function"]["name"] for c in reply.tool_calls], ["get_weather", "search"]
        )
        self.assertNotEqual(reply.tool_calls[0]["id"], reply.tool_calls[1]["id"])

    def test_braces_inside_arguments_are_preserved(self):
        arguments = {"q": "a } b { c"}
        reply = parse(envelope(calls=[{"name": "search", "arguments": arguments}]))
        self.assertEqual(
            json.loads(reply.tool_calls[0]["function"]["arguments"]), arguments
        )


class AnswerTests(unittest.TestCase):
    def test_normal_answer(self):
        reply = parse(envelope(content="Beijing is warm today."))
        self.assertEqual(reply.content, "Beijing is warm today.")
        self.assertEqual(reply.tool_calls, [])

    def test_empty_answer_string_is_allowed(self):
        reply = parse(envelope(content=""))
        self.assertEqual(reply.content, "")
        self.assertEqual(reply.tool_calls, [])


class StrictnessTests(unittest.TestCase):
    def assert_invalid(self, raw, **kw):
        with self.assertRaises(ToolOutputError):
            parse(raw, **kw)

    def test_invalid_json(self):
        self.assert_invalid("{not json at all}")

    def test_plain_prose_is_not_json(self):
        self.assert_invalid("Beijing is warm today.")

    def test_legacy_fenced_tool_call_is_rejected(self):
        self.assert_invalid(
            '```tool_call\n{"name":"get_weather","arguments":{"city":"Oslo"}}\n```'
        )

    def test_extra_envelope_key(self):
        self.assert_invalid(
            '{"content":null,"tool_calls":[],"extra":1}'
        )

    def test_missing_content_key(self):
        self.assert_invalid('{"tool_calls":[]}')

    def test_duplicate_json_key(self):
        self.assert_invalid('{"content":"a","content":"b","tool_calls":[]}')

    def test_nan_constant(self):
        self.assert_invalid('{"content":NaN,"tool_calls":[]}')

    def test_normal_answer_needs_string_content(self):
        self.assert_invalid('{"content":null,"tool_calls":[]}')

    def test_tool_call_must_null_content(self):
        self.assert_invalid(envelope(content="ok", calls=[
            {"name": "get_weather", "arguments": {"city": "Oslo"}}
        ]))

    def test_unknown_tool(self):
        self.assert_invalid(envelope(calls=[{"name": "rm_rf", "arguments": {}}]))

    def test_arguments_must_be_an_object(self):
        self.assert_invalid(envelope(calls=[{"name": "get_weather", "arguments": "Oslo"}]))

    def test_call_must_have_exactly_name_and_arguments(self):
        self.assert_invalid(envelope(calls=[
            {"name": "get_weather", "arguments": {}, "extra": 1}
        ]))

    def test_required_mode_without_call(self):
        self.assert_invalid(envelope(content="just prose"), mode="required")

    def test_named_mode_rejects_another_tool(self):
        self.assert_invalid(
            envelope(calls=[{"name": "search", "arguments": {"q": "x"}}]),
            mode="named",
            forced="get_weather",
        )

    def test_parallel_disabled_rejects_two_calls(self):
        self.assert_invalid(envelope(calls=[
            {"name": "get_weather", "arguments": {"city": "Oslo"}},
            {"name": "search", "arguments": {"q": "oslo"}},
        ]), parallel=False)

    def test_parallel_disabled_accepts_one_call(self):
        reply = parse(
            envelope(calls=[{"name": "get_weather", "arguments": {"city": "Oslo"}}]),
            parallel=False,
        )
        self.assertEqual(len(reply.tool_calls), 1)


if __name__ == "__main__":
    unittest.main()
