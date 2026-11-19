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

# The single shape we ask for. Deliberately a fenced block: models keep fences
# intact far more reliably than bare JSON, and it survives surrounding prose.
FORMAT = (
    "```tool_call\n"
    '{"name": "<tool_name>", "arguments": {"<argument>": "<value>"}}\n'
    "```"
)

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
            "- `arguments` must be a JSON object that matches that tool's parameter schema.",
            "- Use the exact tool name from the list. Never invent tools or arguments.",
            "- Do not explain or describe the call: output only the block(s).",
            "- After a tool result is provided, use it to continue the task.",
            "- If no tool is needed, answer normally in plain text instead.",
        ]
        if not self.parallel:
            rules.append("- Call at most one tool per reply.")
        else:
            rules.append(
                "- To call several tools at once, output one block per call, "
                "with no text between the blocks."
            )
        if self.mode == "required":
            rules.append("- You MUST call at least one tool in this reply.")
        elif self.mode == "named" and self.forced:
            rules.append(f"- You MUST call the tool `{self.forced}` in this reply.")

        return (
            "## Tool calling\n\n"
            "You can call the tools listed below. To call a tool, output exactly "
            "one fenced block and nothing else:\n\n"
            f"{FORMAT}\n\n"
            "Rules:\n" + "\n".join(rules) + "\n\n"
            "<available_tools>\n"
            f"{json.dumps(specs, ensure_ascii=False, indent=2)}\n"
            "</available_tools>"
        )

    def reminder(self) -> str:
        """A compact nudge for turns that continue a thread where tools are set."""
        names = ", ".join(sorted(self.allowed)) or "(none)"
        extra = ""
        if self.mode == "required":
            extra = " You MUST call a tool in this reply."
        return (
            f"Tool calling is still enabled (tools: {names}). When you need one, "
            f"reply with a ```tool_call block and nothing else.{extra}"
        )

    def retry_prompt(self) -> str:
        """Sent once when ``tool_choice`` demands a call the model didn't make."""
        if self.mode == "named" and self.forced:
            return (
                f"You did not call `{self.forced}`. Call it now: output only a "
                "```tool_call block."
            )
        return "You did not call a tool. Call one now: output only a ```tool_call block."


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
