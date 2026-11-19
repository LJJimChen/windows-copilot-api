"""Flatten an OpenAI ``messages`` array into a single Copilot prompt.

Copilot's protocol has no role/system channel — it takes one prompt string per
turn — so we collapse messages into one piece of text.

Two shapes are produced:

* :func:`messages_to_prompt` renders the whole conversation. Used when a new
  upstream thread is started (the thread is blank, so it needs all of it).
* :func:`continuation_prompt` renders only what is new. Used when the bridge
  recognized the upstream thread from the resent history, so re-sending the
  whole conversation would duplicate it inside Copilot's own context.
"""

import json
from typing import Any, Dict, List, Optional, Union

from .schemas import ChatMessage, ToolCall

SYSTEM_ROLES = ("system", "developer")


def content_text(content: Optional[Union[str, List[Any], Dict[str, Any]]]) -> str:
    """Extract plain text from a message's content (string or content-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") in (None, "text", "input_text", "output_text"):
                parts.append(part.get("text") or "")
        else:
            parts.append(str(part))
    return "\n".join(p for p in parts if p)


def render_tool_call(call: ToolCall) -> str:
    """Render one assistant tool call in the prompt protocol the model expects."""
    arguments: Any = call.function.arguments
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            pass
    block = {"name": call.function.name, "arguments": arguments if arguments is not None else {}}
    return "```tool_call\n" + json.dumps(block, ensure_ascii=False) + "\n```"


def _tool_names(messages: List[ChatMessage]) -> Dict[str, str]:
    """Map ``tool_call_id`` -> function name, for labelling tool results."""
    names: Dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls or []:
            if call.id:
                names[call.id] = call.function.name
    return names


def render_message(message: ChatMessage, names: Dict[str, str]) -> str:
    role = (message.role or "").lower()
    text = content_text(message.content)

    if role in SYSTEM_ROLES:
        return text
    if role == "user":
        return f"User: {text}"
    if role == "tool":
        call_id = message.tool_call_id or "unknown"
        name = names.get(call_id)
        label = f"Tool result ({call_id}, {name})" if name else f"Tool result ({call_id})"
        return f"{label}: {text}"
    if role == "assistant":
        pieces = [text] if text else []
        pieces.extend(render_tool_call(call) for call in message.tool_calls or [])
        body = "\n".join(pieces)
        return f"Assistant: {body}" if body else "Assistant:"
    return f"{role.capitalize() or 'Message'}: {text}"


def _join(system: Optional[str], inline_system: List[str], body: str) -> str:
    system = "\n\n".join(part for part in [system, *inline_system] if part)
    if system and body:
        return f"{system}\n\n{body}"
    return system or body


def messages_to_prompt(
    messages: List[ChatMessage], system: Optional[str] = None
) -> str:
    """Render the whole conversation for a brand-new upstream thread."""
    inline_system = [
        content_text(m.content)
        for m in messages
        if (m.role or "").lower() in SYSTEM_ROLES and content_text(m.content)
    ]
    convo = [m for m in messages if (m.role or "").lower() not in SYSTEM_ROLES]

    if len(convo) == 1 and convo[0].role == "user" and not convo[0].tool_calls:
        body = content_text(convo[0].content)  # simple single-turn request
    else:
        names = _tool_names(messages)
        lines = [render_message(m, names) for m in convo]
        lines.append("Assistant:")  # cue Copilot to continue
        body = "\n".join(lines)

    return _join(system, inline_system, body)


def continuation_prompt(
    messages: List[ChatMessage], start: int, system: Optional[str] = None
) -> str:
    """Render only ``messages[start:]`` for a thread Copilot already remembers.

    Assistant messages in the new tail are dropped: Copilot itself produced every
    earlier reply, including ``tool_call`` blocks. The only thing the thread
    hasn't seen is the tool *result* (and any new user message). Re-sending an
    assistant turn would make the model think it never ran, or duplicate text.
    """
    tail = [m for m in messages[start:] if (m.role or "").lower() not in SYSTEM_ROLES]
    names = _tool_names(messages)

    kept: List[ChatMessage] = [m for m in tail if m.role != "assistant"]

    if not kept:
        # Nothing new (a retry/regenerate of the previous turn): re-ask the last
        # user message so the upstream thread sees a fresh turn.
        body = ""
        for message in reversed(messages):
            if message.role == "user" and content_text(message.content):
                body = content_text(message.content)
                break
        body = body or "Continue."
    elif len(kept) == 1 and kept[0].role == "user":
        body = content_text(kept[0].content)  # a plain follow-up question
    else:
        body = "\n".join(render_message(m, names) for m in kept)
        body += "\nAssistant:"

    return _join(system, [], body)
