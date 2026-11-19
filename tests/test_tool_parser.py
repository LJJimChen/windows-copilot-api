"""Tests for the text -> tool_calls parser (the model's only output channel)."""

import json
import unittest

from server.tool_parser import first_marker, parse_tool_calls

ALLOWED = {"get_weather", "search"}


def call(text, allowed=ALLOWED):
    return parse_tool_calls(text, allowed)


class FormatTests(unittest.TestCase):
    def assert_call(self, reply, name, arguments):
        self.assertEqual([c["function"]["name"] for c in reply.tool_calls], [name])
        self.assertEqual(json.loads(reply.tool_calls[0]["function"]["arguments"]), arguments)
        self.assertTrue(reply.tool_calls[0]["id"].startswith("call_"))
        self.assertEqual(reply.tool_calls[0]["type"], "function")

    def test_our_fenced_format(self):
        reply = call('```tool_call\n{"name": "get_weather", "arguments": {"city": "Beijing"}}\n```')
        self.assertIsNone(reply.content)
        self.assert_call(reply, "get_weather", {"city": "Beijing"})

    def test_fence_survives_surrounding_prose(self):
        reply = call(
            "I'll check that.\n```tool_call\n"
            '{"name": "get_weather", "arguments": {"city": "Beijing"}}\n```'
        )
        self.assertEqual(reply.content, "I'll check that.")
        self.assert_call(reply, "get_weather", {"city": "Beijing"})

    def test_hermes_xml(self):
        reply = call('<tool_call>{"name":"get_weather","arguments":{"city":"Oslo"}}</tool_call>')
        self.assertIsNone(reply.content)
        self.assert_call(reply, "get_weather", {"city": "Oslo"})

    def test_mistral_marker(self):
        reply = call('[TOOL_CALLS] [{"name":"get_weather","arguments":{"city":"Oslo"}}]')
        self.assert_call(reply, "get_weather", {"city": "Oslo"})

    def test_llama_marker(self):
        reply = call('<|tool_call|>{"name":"get_weather","arguments":{"city":"Oslo"}}')
        self.assert_call(reply, "get_weather", {"city": "Oslo"})

    def test_deepseek_block(self):
        reply = call(
            "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>get_weather\n"
            '```json\n{"city":"Oslo"}\n```<｜end_of_query｜><｜end_of_query｜>'
        )
        self.assertIsNone(reply.content)
        self.assert_call(reply, "get_weather", {"city": "Oslo"})

    def test_bare_json_without_fence(self):
        reply = call('{"name": "get_weather", "arguments": {"city": "Oslo"}}')
        self.assert_call(reply, "get_weather", {"city": "Oslo"})

    def test_llama_style_parameters_key(self):
        reply = call('{"name":"get_weather","parameters":{"city":"Oslo"}}')
        self.assert_call(reply, "get_weather", {"city": "Oslo"})

    def test_openai_style_tool_calls_wrapper(self):
        reply = call(
            '{"tool_calls":[{"type":"function","function":'
            '{"name":"search","arguments":"{\\"q\\": \\"rust\\"}"}}]}'
        )
        self.assert_call(reply, "search", {"q": "rust"})

    def test_json_envelope_with_parallel_calls(self):
        reply = call(
            '{"tool_calls":['
            '{"name":"get_weather","arguments":{"city":"Oslo"}},'
            '{"name":"search","arguments":{"q":"oslo"}}]}'
        )
        self.assertEqual(
            [c["function"]["name"] for c in reply.tool_calls], ["get_weather", "search"]
        )

    def test_parallel_calls(self):
        reply = call(
            "```tool_call\n"
            '{"name":"get_weather","arguments":{"city":"Oslo"}}\n'
            "```\n```tool_call\n"
            '{"name":"search","arguments":{"q":"weather"}}\n'
            "```"
        )
        self.assertEqual(
            [c["function"]["name"] for c in reply.tool_calls], ["get_weather", "search"]
        )


class ValidationTests(unittest.TestCase):
    def test_unknown_tool_is_left_as_text(self):
        raw = '```tool_call\n{"name":"drop_database","arguments":{}}\n```'
        reply = call(raw)
        self.assertEqual(reply.tool_calls, [])
        self.assertIn("drop_database", reply.content)

    def test_invalid_json_is_left_as_text(self):
        raw = "```tool_call\n{not json at all}\n```"
        reply = call(raw)
        self.assertEqual(reply.tool_calls, [])
        self.assertIn("not json at all", reply.content)

    def test_non_dict_arguments_are_rejected(self):
        reply = call('```tool_call\n{"name":"get_weather","arguments":"Oslo"}\n```')
        self.assertEqual(reply.tool_calls, [])

    def test_no_call_returns_original_text(self):
        reply = call("Just a normal answer, with {braces} in it.")
        self.assertEqual(reply.tool_calls, [])
        self.assertEqual(reply.content, "Just a normal answer, with {braces} in it.")

    def test_unbalanced_prose_brace_does_not_hide_a_later_call(self):
        reply = call('A { stray brace.\n{"name":"get_weather","arguments":{"city":"Oslo"}}')
        self.assertEqual(len(reply.tool_calls), 1)
        self.assertEqual(json.loads(reply.tool_calls[0]["function"]["arguments"]), {"city": "Oslo"})

    def test_truncated_json_does_not_crash(self):
        reply = call('```tool_call\n{"name":"get_weather","arguments":{"city":"Os')
        self.assertEqual(reply.tool_calls, [])

    def test_braces_inside_arguments_do_not_end_the_call(self):
        reply = call(
            '```tool_call\n{"name":"search","arguments":{"q":"a } b { c"}}\n```'
        )
        self.assert_call_arguments(reply, {"q": "a } b { c"})

    def assert_call_arguments(self, reply, expected):
        self.assertEqual(json.loads(reply.tool_calls[0]["function"]["arguments"]), expected)


class StreamingMarkerTests(unittest.TestCase):
    def test_detects_every_opener(self):
        for opener in (
            '{"tool_calls"',
            "```tool_call",
            "<tool_call>",
            "[TOOL_CALLS]",
            "<|tool_call|>",
        ):
            self.assertEqual(first_marker("hello " + opener), 6, opener)

    def test_plain_text_has_no_marker(self):
        self.assertIsNone(first_marker("a normal ```python code block"))


if __name__ == "__main__":
    unittest.main()
