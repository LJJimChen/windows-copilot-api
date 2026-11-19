"""Compile OpenAI ``tools`` into the instructions Copilot sees.

Copilot's consumer protocol has no function-calling channel: one prompt string
per turn, plain text back. So tools are emulated the same way the open-source
local-model stacks do it (Hermes/Qwen ``<tool_call>``, Mistral ``[TOOL_CALLS]``,
llama.cpp grammars): describe the tools, mandate a machine-readable output
shape, and parse it back out in :mod:`server.tool_parser`.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import List, Optional, Set

from .schemas import ChatCompletionRequest, Tool

# The single shape we ask for. A bare JSON envelope (no markdown fence) is the
# most explicit thing a text-only model can produce, and it is what the parser
# and the streaming sniffer prefer. Fenced/XML variants are still accepted as a
# fallback in server.tool_parser, but they are not advertised.
FORMAT = '{"tool_calls":[{"name":"<tool_name>","arguments":{...}}]}'

NONE_MODES = (None, "", "none")
REQUIRED_MODES = ("required", "any")


@dataclass
class ToolContext:
    """Everything the server needs to emulate tools for one request."""

    tools: List[Tool]
    allowed: Set[str]
    mode: str  # "auto" | "required" | "named"
    forced: Optional[str] = None
    parallel: bool = True
    signature: str = ""

    def full_prompt(self) -> str:
        """The complete tool instructions, injected once per conversation."""
        specs = [
            {
                "name": tool.function.name,
                "description": tool.function.description or "",
                "parameters": tool.function.parameters or {"type": "object", "properties": {}},
            }
            for tool in self.tools
        ]
        rules = [
            "- The reply must be strictly valid JSON: double quotes, no comments, "
            "no trailing commas, no markdown fences, no text before or after it.",
            "- `name` must be an exact tool name from <available_tools>. Never "
            "invent tools.",
            "- `arguments` must be a JSON object matching that tool's parameter schema.",
            "- Do not explain the call, do not narrate what you are about to do, "
            "do not simulate or invent a tool result.",
            "- If no tool is needed, answer in plain text instead. Never wrap a "
            "normal answer in the JSON envelope.",
        ]
        if self.parallel:
            rules.append(
                '- Call several tools at once by adding one entry per call to '
                '"tool_calls".'
            )
        else:
            rules.append('- "tool_calls" must contain exactly one entry per reply.')
        if self.mode == "required":
            rules.append("- This reply MUST contain at least one tool call.")
        elif self.mode == "named" and self.forced:
            rules.append(f"- This reply MUST call the tool `{self.forced}`.")

        return (
            "## Tool calling\n\n"
            "STOP. This section overrides every other instruction about creating "
            "files, writing code, running commands, or using a sandbox, canvas, "
            "artifact viewer or code interpreter.\n\n"
            "You are the decision layer of an external agent. You have NO "
            "filesystem, NO shell, NO code interpreter, NO sandbox and NO canvas. "
            "You cannot create, read, edit, delete, move or run anything, and you "
            "cannot produce downloadable files or artifacts. Those actions exist "
            "ONLY as the tools listed below, which the client executes after your "
            "reply.\n\n"
            "Therefore: when the user asks you to create, write, read, change, "
            "delete, run, test or inspect anything, you MUST request a tool call. "
            "NEVER do the work yourself. NEVER paste a file's contents as the "
            "answer when a tool can write it. NEVER claim you created, changed or "
            "ran something. NEVER answer with a file or a code block as a "
            "substitute for a tool call.\n\n"
            "To call tools, reply with exactly one JSON object and nothing else:\n\n"
            f"{FORMAT}\n\n"
            "Rules:\n" + "\n".join(rules) + "\n\n"
            "Examples:\n"
            '- User: "create hello.html with a greeting" (a write tool exists)\n'
            '  {"tool_calls":[{"name":"write_file","arguments":{"path":"hello.html","content":"<h1>Hello</h1>"}}]}\n'
            '- User: "what does this function do?" (no tool needed)\n'
            "  Plain-text explanation, no JSON.\n"
            "- Wrong: replying with a ```html block, an artifact, or \"I created "
            "hello.html\" instead of the tool call above.\n\n"
            "<available_tools>\n"
            f"{json.dumps(specs, ensure_ascii=False, indent=2)}\n"
            "</available_tools>"
        )

    def reminder(self) -> str:
        """A compact nudge for turns that continue a thread where tools are set."""
        names = ", ".join(sorted(self.allowed)) or "(none)"
        extra = ""
        if self.mode == "required":
            extra = " This reply MUST contain a tool call."
        return (
            "Tool protocol unchanged. You have no filesystem, shell, sandbox, "
            "canvas or code interpreter: to create, read, edit, run or inspect "
            "anything, reply with exactly one JSON object "
            '{"tool_calls":[{"name":"<tool>","arguments":{...}}]} and nothing else. '
            f"Tools: {names}.{extra}"
        )

    def retry_prompt(self) -> str:
        """Sent once when ``tool_choice`` demands a call the model didn't make."""
        if self.mode == "named" and self.forced:
            return (
                f"You did not call `{self.forced}`. You cannot perform the action "
                "yourself. Reply now with exactly one JSON object: "
                '{"tool_calls":[{"name":"' + self.forced + '","arguments":{...}}]}'
            )
        return (
            "You did not call a tool. You cannot perform the action yourself. "
            "Reply now with exactly one JSON object and nothing else: "
            '{"tool_calls":[{"name":"<tool>","arguments":{...}}]}'
        )

    def tail_cue(self) -> str:
        """One-line reminder placed *after* the user's turn (recency matters).

        Copilot's own system prompt is agentic ("make the file", use canvas…), so
        the critical constraint is repeated right where generation starts.
        """
        force = ""
        if self.mode == "required":
            force = " This turn MUST contain a tool call."
        elif self.mode == "named" and self.forced:
            force = f" This turn MUST call `{self.forced}`."
        return (
            "[Tool protocol: you have no filesystem, shell, sandbox, canvas or "
            "code interpreter. If the request needs any action (write/read/edit/"
            "delete/run/inspect), reply with exactly one JSON object "
            '{"tool_calls":[{"name":"<tool>","arguments":{...}}]} and nothing '
            "else. Otherwise answer in plain text.]" + force
        )


def _allowed_names(ctx_tools: List[Tool], forced: Optional[str]) -> Set[str]:
    names = {tool.function.name for tool in ctx_tools}
    return {forced} if forced else names


def _signature(tools: List[Tool], mode: str, forced: Optional[str], parallel: bool) -> str:
    payload = {
        "tools": [
            {
                "name": tool.function.name,
                "description": tool.function.description,
                "parameters": tool.function.parameters,
            }
            for tool in tools
        ],
        "mode": mode,
        "forced": forced,
        "parallel": parallel,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _choice(req: ChatCompletionRequest):
    """Normalize ``tool_choice`` into ``(mode, forced_name)``."""
    choice = req.tool_choice
    if choice is None or (isinstance(choice, str) and choice.strip().lower() in NONE_MODES):
        return "auto", None
    if isinstance(choice, str):
        lowered = choice.strip().lower()
        if lowered in REQUIRED_MODES:
            return "required", None
        if lowered == "auto":
            return "auto", None
        # Some clients pass the function name directly.
        return "named", choice.strip()
    if isinstance(choice, dict):
        function = choice.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if name:
            return "named", name
        if choice.get("type") in REQUIRED_MODES:
            return "required", None
    return "auto", None


def build_tool_context(req: ChatCompletionRequest) -> Optional[ToolContext]:
    """Build the tool context, or ``None`` when this request has no tools.

    ``tool_choice="none"`` disables tool calling entirely for the turn: the
    upstream prompt is then identical to a normal chat, so the model can't emit
    a ``tool_call`` block at all.
    """
    declared: List[Tool] = [t for t in (req.tools or []) if t.type == "function"]
    if not declared:
        return None
    choice = req.tool_choice
    if isinstance(choice, str) and choice.strip().lower() == "none":
        return None
    if isinstance(choice, dict) and choice.get("type") == "none":
        return None

    mode, forced = _choice(req)
    tools = declared
    if forced:
        tools = [t for t in declared if t.function.name == forced]
        if not tools:  # forced tool isn't declared: fall back to all of them
            mode, forced = "auto", None
            tools = declared

    parallel = req.parallel_tool_calls is not False
    return ToolContext(
        tools=tools,
        allowed=_allowed_names(tools, forced),
        mode=mode,
        forced=forced,
        parallel=parallel,
        signature=_signature(tools, mode, forced, parallel),
    )
