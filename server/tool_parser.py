"""Parse the model's JSON routing decision into OpenAI tool calls.

Copilot has no function-calling channel, so :mod:`server.tool_prompt` asks for
exactly one JSON object per turn:

    {"content": "answer or null",
     "tool_calls": [{"name": "function_name", "arguments": {...}}]}

This module is deliberately strict: anything that does not match that envelope
exactly raises :class:`ToolOutputError`. The caller retries once with a
corrective prompt and then falls back to returning the text, so a malformed or
hallucinated reply can never reach the client as a fake tool call.

Being strict is also what makes the routing protocol safe: the model cannot
smuggle a call in prose, a markdown fence, or an undeclared function name.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

ENVELOPE_KEYS = {"content", "tool_calls"}
CALL_KEYS = {"name", "arguments"}


class ToolOutputError(ValueError):
    """The reply was not a valid routing envelope."""


@dataclass
class ParsedReply:
    """One validated upstream reply."""

    content: Optional[str]
    tool_calls: List[Dict[str, Any]]
    raw: str = ""

    @property
    def has_calls(self) -> bool:
        return bool(self.tool_calls)


def _reject_constant(value: str) -> Any:
    """``json.loads`` hook: NaN/Infinity are not valid JSON."""
    raise ValueError(f"non-standard JSON constant {value!r}")


def _unique_object(pairs: List[tuple]) -> Dict[str, Any]:
    """``json.loads`` hook: reject duplicate object keys."""
    data: Dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key {key!r}")
        data[key] = value
    return data


def _strip_fence(text: str) -> str:
    """Allow the object to be wrapped in a single ```json fence."""
    raw = (text or "").strip()
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip() in ("```", "```json"):
            raw = "\n".join(lines[1:-1]).strip()
    return raw


def parse_tool_output(
    text: str,
    *,
    allowed: Set[str],
    mode: str = "auto",
    forced: Optional[str] = None,
    parallel: bool = True,
) -> ParsedReply:
    """Validate one reply and convert it into OpenAI ``tool_calls``.

    ``mode`` is ``"auto"``, ``"required"`` or ``"named"``; ``forced`` is the
    function name for the latter. Raises :class:`ToolOutputError` on any
    deviation from the protocol.
    """
    raw = _strip_fence(text)
    try:
        data = json.loads(
            raw,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except ValueError as exc:
        raise ToolOutputError("the reply is not valid JSON") from exc

    if not isinstance(data, dict) or set(data) != ENVELOPE_KEYS:
        raise ToolOutputError(
            'the reply must be an object with exactly "content" and "tool_calls"'
        )

    content, calls = data["content"], data["tool_calls"]
    if not isinstance(calls, list) or not (content is None or isinstance(content, str)):
        raise ToolOutputError(
            '"content" must be a string or null and "tool_calls" must be an array'
        )
    if not calls and not isinstance(content, str):
        raise ToolOutputError('a normal answer needs a string "content"')
    if calls and content is not None:
        raise ToolOutputError('a tool-call reply must set "content" to null')
    if mode == "none" and calls:
        raise ToolOutputError("the model called a tool despite tool_choice='none'")
    if (mode == "required" or mode == "named") and not calls:
        raise ToolOutputError("the model did not produce the required tool call")
    if not parallel and len(calls) > 1:
        raise ToolOutputError(
            "the model returned multiple calls while parallel_tool_calls is false"
        )

    converted: List[Dict[str, Any]] = []
    for call in calls:
        if (
            not isinstance(call, dict)
            or set(call) != CALL_KEYS
            or not isinstance(call["name"], str)
            or not isinstance(call["arguments"], dict)
        ):
            raise ToolOutputError(
                'each call must be {"name": "<declared function>", "arguments": {...}}'
            )
        name = call["name"]
        if name not in allowed:
            raise ToolOutputError(f"the model called an undeclared function {name!r}")
        if mode == "named" and forced and name != forced:
            raise ToolOutputError(f"the model called {name!r} instead of {forced!r}")
        converted.append({
            "id": f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
            },
        })

    return ParsedReply(content, converted, text or "")
