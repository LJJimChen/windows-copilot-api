"""Automatic conversation reuse ("plan C").

OpenAI clients are stateless: they resend the whole conversation on every call.
Copilot is stateful: one conversation id remembers earlier turns. Re-sending the
whole history to a thread that already has it duplicates context (and burns the
model's window), so the bridge fingerprints the incoming messages and looks for
a conversation whose history is a prefix of them:

    incoming history matches a known thread + has a new tail
        -> continue that thread and send only the tail

    history diverged (edited, trimmed, compacted, branched) or is unknown
        -> start a fresh thread with the whole conversation

Matching on *history* rather than on a client-supplied id means it works with
any client, including ones that rewrite history during context compaction.

Everything here is in-memory and best-effort: the store is a personal-use cache,
not a database. A miss just means "start a new conversation", which is always
correct, only slightly more expensive.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from .config import SESSION_MAX, SESSION_REGENERATE_WINDOW, SESSION_TTL
from .prompt import content_text
from .schemas import ChatMessage


def message_fingerprint(message: ChatMessage) -> str:
    """A stable hash of everything in a message that affects the conversation."""
    parts = [
        (message.role or "").lower(),
        content_text(message.content),
        message.tool_call_id or "",
        message.name or "",
    ]
    if message.tool_calls:
        calls = []
        for call in message.tool_calls:
            arguments = call.function.arguments
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    pass
            calls.append([call.function.name, arguments])
        parts.append(json.dumps(calls, sort_keys=True, ensure_ascii=False, default=str))
    raw = "\x1f".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def history_fingerprints(messages: Iterable[ChatMessage]) -> Tuple[str, ...]:
    return tuple(message_fingerprint(message) for message in messages)


@dataclass
class ConversationState:
    conversation_id: str
    fingerprints: Tuple[str, ...]
    tool_sig: Optional[str]
    updated: float


class ConversationStore:
    """Small LRU + TTL cache of ``history prefix -> upstream conversation``."""

    def __init__(self, ttl: float = SESSION_TTL, max_entries: int = SESSION_MAX):
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, ConversationState]" = OrderedDict()

    def match(
        self, fingerprints: Tuple[str, ...], now: Optional[float] = None
    ) -> Optional[ConversationState]:
        """Return the conversation whose history is the longest prefix, if any."""
        if not fingerprints:
            return None
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            best: Optional[ConversationState] = None
            for state in self._items.values():
                size = len(state.fingerprints)
                if size == 0 or size > len(fingerprints):
                    continue
                if state.fingerprints != fingerprints[:size]:
                    continue
                if size == len(fingerprints) and (
                    now - state.updated > SESSION_REGENERATE_WINDOW
                ):
                    # Same history as an old turn: almost certainly a fresh
                    # conversation that happens to start with the same message
                    # ("hi"), not a regenerate. Only the recent case is a retry.
                    continue
                if best is None or size >= len(best.fingerprints):
                    best = state
            if best is not None:
                self._items.move_to_end(best.conversation_id)
            return best

    def record(
        self,
        conversation_id: str,
        fingerprints: Tuple[str, ...],
        tool_sig: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        stamp = time.time() if now is None else now
        with self._lock:
            self._items[conversation_id] = ConversationState(
                conversation_id, tuple(fingerprints), tool_sig, stamp
            )
            self._items.move_to_end(conversation_id)
            self._prune(stamp)

    def forget(self, conversation_id: str) -> None:
        with self._lock:
            self._items.pop(conversation_id, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _prune(self, now: float) -> None:
        stale = [
            cid for cid, state in self._items.items()
            if now - state.updated > self._ttl
        ]
        for cid in stale:
            self._items.pop(cid, None)
        while len(self._items) > self._max:
            self._items.popitem(last=False)
