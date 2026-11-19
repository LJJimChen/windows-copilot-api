"""Pydantic request models for the OpenAI-compatible endpoints."""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel

from .config import MODEL_NAME


class FunctionCall(BaseModel):
    """The ``function`` object of an assistant tool call.

    ``arguments`` is a JSON *string* on the OpenAI wire, but some clients send an
    object; both are accepted and normalized when the prompt is built.
    """

    name: str
    arguments: Optional[Union[str, Dict[str, Any]]] = None


class ToolCall(BaseModel):
    id: Optional[str] = None
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    role: str
    # content is a plain string, OpenAI "content parts" (list of dicts), or null
    # for some tool/assistant messages.
    content: Optional[Union[str, List[Any]]] = None
    name: Optional[str] = None
    # Assistant turns that requested tools, and the results the client sends back.
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None


class FunctionDef(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    strict: Optional[bool] = None


class Tool(BaseModel):
    type: str = "function"
    function: FunctionDef


class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = MODEL_NAME
    stream: bool = False
    # Copilot's own conversation id (returned in earlier responses). Optional:
    # without it the bridge fingerprints the resent history and reuses the
    # matching upstream conversation automatically. Outside OpenAI's schema, but
    # standard clients can set it via extra_body.
    conversation_id: Optional[str] = None
    # Tool calling is emulated with a prompt protocol (Copilot has no native
    # function-calling channel); these fields drive the injected instructions
    # and the parser.
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None
    # Any other OpenAI fields (temperature, max_tokens, ...) are accepted and
    # ignored — Copilot's consumer protocol doesn't expose those knobs.
