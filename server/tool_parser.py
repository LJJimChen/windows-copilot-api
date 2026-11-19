"""Parse tool calls out of plain-text model output.

Copilot answers with text only, so the bridge asks for a fenced ``tool_call``
block (see :mod:`server.tool_prompt`) and turns it back into OpenAI
``tool_calls`` here. Because models drift, the parser accepts the shapes used by
the common open-source tool parsers (vLLM's Hermes/Qwen, Mistral, llama.cpp,
DeepSeek), in a fixed priority order:

    ```tool_call {"name": ..., "arguments": {...}} ```   our preferred format
    <tool_call>{...}</tool_call>                         Hermes / Qwen
    [TOOL_CALLS] [{...}]                                 Mistral
    <|tool_call|>{...}                                   Llama 3.x style
    <｜tool▁calls▁begin｜>function<｜tool▁sep｜>name ...          DeepSeek
    a bare JSON object/array with name + arguments       last resort

Anything that isn't a valid, declared tool call is left alone (and the message
is returned as plain text), so a hallucinated call can never reach the client.
"""

import ast
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Cheap "did the model start emitting a tool call?" probes, used by the
# streaming path to decide whether it must buffer the rest of the turn. Keep
# every entry shorter than ``server.api.TOOL_HOLD``.
OPENERS: Tuple[str, ...] = (
    "```tool_call",
    "<tool_call>",
    "[TOOL_CALLS]",
    "<|tool_call|>",
    "<｜tool▁calls▁begin｜>",
)

_CLOSERS = ("</tool_call>", "<|/tool_call|>", "<｜tool▁calls▁end｜>", "<｜end_of_query｜>")


@dataclass
class ParsedReply:
    """Result of parsing one upstream reply."""

    content: Optional[str]
    tool_calls: List[Dict[str, Any]]
    raw: str = ""

    @property
    def has_calls(self) -> bool:
        return bool(self.tool_calls)


def first_marker(text: str) -> Optional[int]:
    """Index of the earliest tool-call opener in ``text``, or ``None``."""
    found = [idx for idx in (text.find(opener) for opener in OPENERS) if idx != -1]
    return min(found) if found else None


def parse_tool_calls(text: str, allowed: Optional[Iterable[str]] = None) -> ParsedReply:
    """Split ``text`` into assistant content + validated OpenAI ``tool_calls``."""
    allowed_set: Optional[Set[str]] = set(allowed) if allowed else None
    spans = _collect_spans(text or "", allowed_set)

    calls: List[Dict[str, Any]] = []
    chunks: List[str] = []
    cursor = 0
    for start, end, found in spans:
        if start > cursor:
            chunks.append(text[cursor:start])
        cursor = max(cursor, end)
        calls.extend(found)
    if cursor < len(text):
        chunks.append(text[cursor:])

    content = _clean_content("".join(chunks))
    return ParsedReply(content, calls, text or "")


# --------------------------------------------------------------------------- #
# Span extraction
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```[ \t]*(tool_call|tool_calls|json)?[ \t]*\r?\n?(.*?)```", re.S)
_XML_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)
_DEEPSEEK_NAME_RE = re.compile(r"function<｜tool▁sep｜>\s*([^\s`\n]+)")


def _collect_spans(text: str, allowed: Optional[Set[str]]) -> List[Tuple[int, int, List[dict]]]:
    """Find tool-call regions, most specific extractor first, without overlap."""
    candidates: List[Tuple[int, int, List[dict]]] = []

    # 1. fenced blocks. Tagged tool_call blocks are trusted; a generic ```json
    #    fence only counts when it actually looks like a call.
    for match in _FENCE_RE.finditer(text):
        tag = (match.group(1) or "").lower()
        calls = _calls_from_body(match.group(2), allowed, trusted=tag != "json")
        if calls:
            candidates.append((match.start(), match.end(), calls))

    # 2. <tool_call>...</tool_call> (Hermes, Qwen, ...)
    for match in _XML_RE.finditer(text):
        calls = _calls_from_body(match.group(1), allowed, trusted=True)
        if calls:
            candidates.append((match.start(), match.end(), calls))

    # 3. [TOOL_CALLS] followed by a JSON array/object (Mistral).
    candidates.extend(_marker_spans(text, "[TOOL_CALLS]", allowed))

    # 4. <|tool_call|> followed by a JSON object (Llama 3.x style).
    candidates.extend(_marker_spans(text, "<|tool_call|>", allowed))

    # 5. DeepSeek function blocks.
    candidates.extend(_deepseek_spans(text, allowed))

    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    spans: List[Tuple[int, int, List[dict]]] = []
    for start, end, calls in candidates:
        if any(start < existing_end and existing_start < end
               for existing_start, existing_end, _ in spans):
            continue
        spans.append((start, end, calls))

    # 6. Last resort: a bare JSON object/array that names a declared tool. Only
    #    outside regions already claimed above, so fenced/tagged calls aren't
    #    parsed twice.
    for start, end, raw in _balanced(text):
        if any(start < s_end and s_start < end for s_start, s_end, _ in spans):
            continue
        calls = _calls_from_body(raw, allowed, trusted=False)
        if calls:
            spans.append((start, end, calls))

    spans.sort(key=lambda item: item[0])
    return spans


def _marker_spans(text: str, marker: str, allowed: Optional[Set[str]]):
    spans = []
    cursor = 0
    while True:
        idx = text.find(marker, cursor)
        if idx == -1:
            return spans
        payload_start = idx + len(marker)
        found = next(_balanced(text, payload_start), None)
        if found is None:
            cursor = payload_start
            continue
        start, end, raw = found
        calls = _calls_from_body(raw, allowed, trusted=True)
        if calls:
            spans.append((idx, end, calls))
        cursor = end


def _deepseek_spans(text: str, allowed: Optional[Set[str]]):
    spans = []
    for match in _DEEPSEEK_NAME_RE.finditer(text):
        name = match.group(1).strip()
        brace = text.find("{", match.end())
        if brace == -1:
            continue
        found = next(_balanced(text, brace), None)
        if found is None:
            continue
        _, end, raw = found
        obj = _loads(raw)
        if not isinstance(obj, dict):
            continue
        calls = _normalize({"name": name, "arguments": obj}, allowed)
        if calls:
            # Include the surrounding wrapper tokens when they are right there.
            start = match.start()
            for opener in ("<｜tool▁call▁begin｜>", "<｜tool▁calls▁begin｜>"):
                pos = text.rfind(opener, 0, start)
                if pos != -1:
                    start = pos
                    break
            # The call's JSON is usually wrapped in a ```json fence inside the
            # block; swallow the closing fence that follows the object.
            closing = re.match(r"\s*```", text[end:])
            if closing:
                end += closing.end()
            spans.append((start, end, calls))
    return spans


def _calls_from_body(body: str, allowed: Optional[Set[str]], trusted: bool) -> List[dict]:
    """Normalize every call-shaped object/array found in ``body``."""
    calls: List[dict] = []
    for _, _, raw in _balanced(body):
        obj = _loads(raw)
        for candidate in _as_candidates(obj):
            if not trusted and not _looks_like_call(candidate, allowed):
                continue
            calls.extend(_normalize(candidate, allowed))
    return calls


def _as_candidates(obj: Any) -> List[Any]:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return [obj]
    return []


def _looks_like_call(obj: Any, allowed: Optional[Set[str]]) -> bool:
    """Conservative test for un-tagged JSON found in the wild."""
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("tool_calls"), list):
        return True
    function = obj.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return True
    name = obj.get("name")
    if not isinstance(name, str):
        return False
    if "arguments" in obj or "parameters" in obj:
        return True
    # A no-argument tool is only believable when we actually declared that name.
    return bool(allowed and name in allowed)


def _normalize(obj: Any, allowed: Optional[Set[str]]) -> List[dict]:
    """Turn one object into OpenAI-shaped tool calls, or ``[]``."""
    if not isinstance(obj, dict):
        return []
    if isinstance(obj.get("tool_calls"), list):
        calls: List[dict] = []
        for inner in obj["tool_calls"]:
            calls.extend(_normalize(inner, allowed))
        return calls

    function = obj.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments", function.get("parameters", {}))
    else:
        name = obj.get("name")
        arguments = obj.get("arguments", obj.get("parameters", {}))

    if not isinstance(name, str) or not name.strip():
        return []
    name = name.strip()
    if allowed is not None and name not in allowed:
        return []  # hallucinated tool: drop it instead of forwarding

    if isinstance(arguments, str):
        parsed = _loads(arguments)
        if parsed is None:
            return []  # unparseable argument string: not a usable call
        arguments = parsed
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return []

    return [{
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }]


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #

def _loads(raw: str) -> Any:
    """Parse JSON with the leniency local models need (single quotes, comments …)."""
    raw = raw.strip()
    for attempt in (
        raw,
        re.sub(r",\s*([}\]])", r"\1", raw),  # trailing commas
    ):
        try:
            return json.loads(attempt)
        except Exception:
            pass
    try:
        return ast.literal_eval(raw)  # Python literals: single quotes, True/None
    except Exception:
        return None


def _balanced(text: str, start: int = 0):
    """Yield ``(start, end, substring)`` for top-level JSON objects/arrays.

    Tracks nesting and string escapes, so braces inside argument strings don't
    end the scan early. An unbalanced ``{`` (ordinary prose, a truncated stream)
    is skipped rather than aborting the scan, so a later valid object is still
    found.
    """
    index = start
    length = len(text)
    while index < length:
        if text[index] not in "{[":
            index += 1
            continue
        depth = 0
        quote = None
        escaped = False
        cursor = index
        closed = False
        while cursor < length:
            current = text[cursor]
            if quote:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    quote = None
            elif current in "\"'":
                quote = current
            elif current in "{[":
                depth += 1
            elif current in "}]":
                depth -= 1
                if depth == 0:
                    yield index, cursor + 1, text[index:cursor + 1]
                    index = cursor + 1
                    closed = True
                    break
            cursor += 1
        if not closed:
            index += 1  # unbalanced opener: skip it and keep scanning


def _clean_content(text: str) -> Optional[str]:
    """Drop leftover sentinel tokens/fences and normalize whitespace."""
    for token in OPENERS:
        text = text.replace(token, "")
    for token in _CLOSERS:
        text = text.replace(token, "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # A fence that only wrapped a call can leave an empty ``` pair behind.
    text = re.sub(r"(?m)^```[ \t]*$", "", text).strip()
    return text or None
