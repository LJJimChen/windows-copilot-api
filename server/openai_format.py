"""Builders for the OpenAI wire shapes (completions, SSE chunks)."""

import json
import time
import uuid
from typing import Any, Dict, List, Optional


def new_id() -> str:
    """A fresh ``chatcmpl-...`` id, as the OpenAI API returns."""
    return f"chatcmpl-{uuid.uuid4().hex}"


def completion_response(
    text: Optional[str],
    model: str,
    conversation_id=None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """A non-streaming ``chat.completion`` object.

    ``conversation_id`` is Copilot's own conversation id, surfaced as an extra
    top-level field (not part of OpenAI's schema, so standard clients ignore it)
    for callers that want to track the upstream thread.

    When the model asked for tools, ``content`` is typically ``null`` and
    ``finish_reason`` is ``"tool_calls"``, exactly like OpenAI.
    """
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": text if text else (None if tool_calls else ""),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "conversation_id": conversation_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def sse_event(payload: dict) -> str:
    """Serialize a payload as a Server-Sent Events ``data:`` line."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stream_chunk(
    cid: str, created: int, model: str, delta: dict, finish=None, conversation_id=None
) -> dict:
    """A single ``chat.completion.chunk`` object for streaming responses.

    ``conversation_id`` (Copilot's upstream id) is added as an extra top-level
    field when known — typically only on the final chunk, since a new
    conversation's id isn't available until the stream has started.
    """
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if conversation_id is not None:
        chunk["conversation_id"] = conversation_id
    return chunk


def tool_call_deltas(cid: str, created: int, model: str, calls: List[Dict[str, Any]]):
    """Yield the SSE chunks that announce ``calls`` to a streaming client.

    Tool arguments are complete only once the whole upstream turn has been
    buffered, so each call is announced (id + name) and then its full arguments
    is sent in a second chunk. That is valid OpenAI streaming and every client
    that supports tool calls accepts it.
    """
    for index, call in enumerate(calls):
        function = call.get("function") or {}
        yield stream_chunk(cid, created, model, {
            "tool_calls": [{
                "index": index,
                "id": call.get("id"),
                "type": "function",
                "function": {"name": function.get("name", ""), "arguments": ""},
            }]
        })
        yield stream_chunk(cid, created, model, {
            "tool_calls": [{
                "index": index,
                "function": {"arguments": function.get("arguments", "{}")},
            }]
        })
