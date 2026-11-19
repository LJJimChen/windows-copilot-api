"""FastAPI app wiring Copilot onto the OpenAI Chat Completions API.

Two emulation layers live here:

* **Conversation reuse** — clients resend the full history each call (OpenAI's
  API is stateless), while Copilot's threads are stateful. :mod:`server.sessions`
  fingerprints the resent history and, when it matches a known thread, only the
  new tail is sent upstream.
* **Tool calling** — Copilot answers with text only, so tools are emulated with a
  prompt protocol (:mod:`server.tool_prompt`) and parsed back into OpenAI
  ``tool_calls`` (:mod:`server.tool_parser`).
"""

import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from copilot import CopilotClient
from copilot.driver import ClearanceRequired, LicenseRequired

from .config import MODEL_NAME, RATE_LIMIT_BURST, RATE_LIMIT_RPM
from .openai_format import (
    completion_response,
    new_id,
    sse_event,
    stream_chunk,
    tool_call_deltas,
)
from .prompt import continuation_prompt, messages_to_prompt
from .ratelimit import TokenBucket
from .schemas import ChatCompletionRequest
from .sessions import ConversationStore, history_fingerprints
from .tool_parser import first_marker, parse_tool_calls
from .tool_prompt import ToolContext, build_tool_context

app = FastAPI(title="Copilot OpenAI-compatible API", version="1.1.0")
# Server must never pop a visible browser mid-request: chat and token refresh
# are plain HTTP, and if the session is gone entirely the request fails with a
# message to run `python -m copilot login` out of band.
client = CopilotClient(interactive_clear=False)

# How many trailing characters the streaming tool-call sniffer withholds before
# flushing text to the client. Must exceed the longest tool-call opener so a
# marker split across chunks is never emitted as content.
TOOL_HOLD = 32

# history fingerprint -> upstream conversation, in memory (see server/sessions.py).
_sessions = ConversationStore()

_CLEARANCE_HELP = (
    "Copilot asked for human verification before answering. Open copilot.com in "
    "your browser, pass the check, then retry (or run `python -m copilot login`)."
)

_LICENSE_HELP = (
    "This account isn't licensed for Copilot chat. Personal accounts use the free "
    "tier; work/school accounts need a Microsoft 365 Copilot licence, and the "
    "tenant may have turned Copilot off. If this is a work account, also make sure "
    "the server runs with COPILOT_PROFILE=enterprise."
)

# Self-imposed rate limit on top of the concurrency lock below: this caps
# requests-per-minute, the lock caps requests-in-flight. See server/ratelimit.py.
_rate_limiter = TokenBucket(RATE_LIMIT_RPM, RATE_LIMIT_BURST)


def _rate_limited_response():
    """Spend a token; return an OpenAI-shaped 429 if none left, else ``None``."""
    allowed, wait = _rate_limiter.try_acquire()
    if allowed:
        return None
    secs = max(1, round(wait))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(secs)},
        content={"error": {
            "message": (
                f"Rate limit exceeded (>{RATE_LIMIT_RPM:g} req/min). "
                f"Retry in {secs}s."
            ),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }},
    )

# Copilot's per-account chat socket doesn't tolerate concurrent conversations
# from one process (parallel requests error out or hang). This server bridges a
# single signed-in account, so we serialize upstream calls: concurrent HTTP
# requests queue here and run one at a time. Predictable, at the cost of
# parallelism — fine for a personal bridge.
_upstream_lock = threading.Lock()


@dataclass
class _Prepared:
    """One request, translated into what Copilot should actually be sent."""

    prompt: str
    conversation_id: Optional[str]
    tools: Optional[ToolContext]
    fingerprints: Tuple[str, ...]


def _prepare(req: ChatCompletionRequest) -> _Prepared:
    """Resolve the upstream thread and render only the part Copilot hasn't seen."""
    fingerprints = history_fingerprints(req.messages)
    ctx = build_tool_context(req)

    state = _sessions.match(fingerprints)
    if state is not None:
        if ctx is None:
            # The client stopped declaring tools: tell the thread, otherwise it
            # may keep emitting tool_call blocks we no longer parse.
            system = (
                "Tool calling is no longer available. Answer in plain text."
                if state.tool_sig else None
            )
        elif state.tool_sig == ctx.signature:
            # Same tools as last turn: a one-line reminder is enough, the full
            # schema is already in the thread.
            system = ctx.reminder()
        else:
            system = ctx.full_prompt()
        prompt = continuation_prompt(
            req.messages, len(state.fingerprints), system=system
        )
        return _Prepared(prompt, state.conversation_id, ctx, fingerprints)

    # New (or explicitly addressed) conversation: it needs the whole history.
    system = ctx.full_prompt() if ctx else None
    prompt = messages_to_prompt(req.messages, system=system)
    return _Prepared(prompt, req.conversation_id, ctx, fingerprints)


def _remember(prepared: _Prepared, conversation_id: Optional[str]) -> None:
    """Cache the history we just sent so the next turn can send only its tail."""
    if not conversation_id:
        return
    _sessions.record(
        conversation_id,
        prepared.fingerprints,
        prepared.tools.signature if prepared.tools else None,
    )


def _limit_calls(calls: List[dict], ctx: Optional[ToolContext]) -> List[dict]:
    if ctx is not None and not ctx.parallel:
        return calls[:1]
    return calls


def _complete(prepared: _Prepared, model: str) -> Tuple[dict, Optional[str]]:
    """Non-streaming turn: run the upstream chat, optionally parse tool calls."""
    ctx = prepared.tools
    reply = client.chat(prepared.prompt, conversation_id=prepared.conversation_id)
    content, calls = reply.text, []
    if ctx is not None:
        parsed = parse_tool_calls(reply.text, ctx.allowed)
        content, calls = parsed.content, parsed.tool_calls
        if ctx.mode == "required" and not calls and reply.conversation_id:
            # tool_choice demanded a call but the model answered in prose. One
            # corrective turn on the same thread; if it still refuses we return
            # the text rather than failing the request.
            retry = client.chat(ctx.retry_prompt(), conversation_id=reply.conversation_id)
            parsed = parse_tool_calls(retry.text, ctx.allowed)
            content, calls = parsed.content, parsed.tool_calls
            reply = retry
        calls = _limit_calls(calls, ctx)
    return completion_response(content, model, reply.conversation_id, calls), reply.conversation_id


def _stream(prepared: _Prepared, model: str):
    """Yield OpenAI ``chat.completion.chunk`` SSE events for one turn."""
    cid = new_id()
    created = int(time.time())
    ctx = prepared.tools
    calls: List[dict] = []
    conversation_id = prepared.conversation_id

    try:
        with _upstream_lock:  # one upstream chat at a time (released on disconnect)
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            stream = client.stream(
                prepared.prompt, conversation_id=prepared.conversation_id
            )

            if ctx is None:
                # No tools: the original pass-through path, unchanged.
                for piece in stream:
                    if isinstance(piece, str) and piece:
                        yield sse_event(
                            stream_chunk(cid, created, model, {"content": piece})
                        )

            elif ctx.mode == "required":
                # A call is mandatory, so nothing useful can be streamed until
                # we know whether the model complied. Buffer the whole turn.
                text = "".join(piece for piece in stream if isinstance(piece, str))
                parsed = parse_tool_calls(text, ctx.allowed)
                calls = parsed.tool_calls
                if not calls and stream.conversation_id:
                    retry = client.chat(
                        ctx.retry_prompt(), conversation_id=stream.conversation_id
                    )
                    parsed = parse_tool_calls(retry.text, ctx.allowed)
                    calls = parsed.tool_calls
                if parsed.content:
                    yield sse_event(
                        stream_chunk(cid, created, model, {"content": parsed.content})
                    )
                calls = _limit_calls(calls, ctx)

            else:
                # auto mode: stream text immediately, but withhold a small tail
                # so an opener split across chunks is detected before leaking.
                buffer = ""
                tool_mode = False
                for piece in stream:
                    if not isinstance(piece, str) or not piece:
                        continue
                    buffer += piece
                    if tool_mode:
                        continue
                    hit = first_marker(buffer)
                    if hit is not None:
                        if hit > 0:
                            yield sse_event(
                                stream_chunk(cid, created, model, {"content": buffer[:hit]})
                            )
                        buffer = buffer[hit:]
                        tool_mode = True
                    elif len(buffer) > TOOL_HOLD:
                        flush = buffer[:-TOOL_HOLD]
                        buffer = buffer[-TOOL_HOLD:]
                        yield sse_event(
                            stream_chunk(cid, created, model, {"content": flush})
                        )
                if tool_mode:
                    parsed = parse_tool_calls(buffer, ctx.allowed)
                    if parsed.content:
                        yield sse_event(
                            stream_chunk(cid, created, model, {"content": parsed.content})
                        )
                    calls = parsed.tool_calls
                elif buffer:
                    yield sse_event(
                        stream_chunk(cid, created, model, {"content": buffer})
                    )
                calls = _limit_calls(calls, ctx)

            conversation_id = stream.conversation_id or conversation_id
            _remember(prepared, conversation_id)

            for chunk in tool_call_deltas(cid, created, model, calls):
                yield sse_event(chunk)
            yield sse_event(
                stream_chunk(
                    cid, created, model, {},
                    finish="tool_calls" if calls else "stop",
                    conversation_id=conversation_id,
                )
            )
    except ClearanceRequired:
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {_CLEARANCE_HELP}]"}, finish="error")
        )
    except LicenseRequired:
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {_LICENSE_HELP}]"}, finish="error")
        )
    except Exception as exc:  # surface errors to the client instead of hanging
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {exc}]"}, finish="error")
        )
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "microsoft"}
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    prepared = _prepare(req)
    if not prepared.prompt.strip():
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "no text content in messages", "type": "invalid_request_error"}},
        )
    model = req.model or MODEL_NAME

    # Enforce the per-minute ceiling before touching the upstream lock, so excess
    # callers get a fast 429 instead of piling up behind the serialized queue.
    limited = _rate_limited_response()
    if limited is not None:
        return limited

    if req.stream:
        return StreamingResponse(
            _stream(prepared, model), media_type="text/event-stream"
        )

    try:
        with _upstream_lock:  # serialize: one upstream chat at a time
            response, conversation_id = _complete(prepared, model)
    except ClearanceRequired:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": _CLEARANCE_HELP, "type": "clearance_required"}},
        )
    except LicenseRequired as exc:
        return JSONResponse(
            status_code=403,
            content={"error": {"message": f"{_LICENSE_HELP} ({exc})", "type": "license_required"}},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )
    _remember(prepared, conversation_id)
    return response


@app.get("/")
def root():
    return {
        "service": "Copilot OpenAI-compatible API",
        "endpoints": ["/v1/models", "/v1/chat/completions"],
        "features": ["streaming", "conversation-reuse", "tool-calling"],
    }
