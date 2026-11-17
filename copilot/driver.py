"""Pure-HTTP Copilot driver.

Speaks copilot.com's chat protocol (a SignalR WebSocket on substrate.office.com,
see :mod:`copilot.protocol`) directly over ``curl_cffi`` — no browser required.
This is the low-level engine; most callers should use
:class:`copilot.client.CopilotClient`. The browser (:mod:`copilot.browser`) is
only used to sign in and, as a fallback, to mint a fresh token.
"""

import time
import uuid
from http.cookiejar import CookieJar
from select import select
from typing import Optional

from curl_cffi.const import CurlECode, CurlInfo
from curl_cffi.curl import CurlError
from curl_cffi.requests import CurlWsFlag, Session

from .models import AbstractProvider, Conversation, ImageType
from .profile import current_profile
from .protocol import (
    HANDSHAKE, PING, TurnParser, build_hub_url, chat_frame, encode, split_records,
)

# curl_cffi's WebSocket.recv() loops on CURLE_AGAIN forever (select() then retry)
# and never returns on an idle socket, so we drive the fragment loop ourselves to
# honour a deadline. CURL_SOCKET_BAD is libcurl's "no active socket" sentinel.
_CURL_SOCKET_BAD = -1


class ClearanceRequired(RuntimeError):
    """copilot.com refused the turn pending a human-verification check.

    The chat backend accepts signed-in turns without one today; this is raised
    if it starts rejecting them with a challenge/captcha result, so callers get
    a clear error instead of a generic failure.
    """


class AuthExpired(RuntimeError):
    """The chat socket rejected the access token (expired or revoked)."""


class LicenseRequired(RuntimeError):
    """The account has no Copilot licence/seat for the requested experience.

    Consumer accounts see this only if the free tier is unavailable; work/school
    accounts see it when the user has no Microsoft 365 Copilot licence (or an
    administrator turned Copilot off for them). Nothing can be fixed client-side:
    the tenant must assign a licence.
    """


class Copilot(AbstractProvider):
    label = "Microsoft Copilot"
    url = "https://copilot.com"
    working = True
    supports_stream = True
    default_model = "Copilot"
    needs_auth = True  # copilot.com only chats when signed in

    def create_completion(
            self,
            prompt: str,
            stream: bool = False,
            proxy: str = None,
            timeout: int = 900,
            image: ImageType = None,
            conversation: Optional[Conversation] = None,
            conversation_id: str = None,
            return_conversation: bool = False,
            access_token: str = None,
            oid: str = None,
            tenant: Optional[str] = None,
            **kwargs
        ):
        """Stream a Copilot reply to ``prompt``.

        Opens the chat hub socket with the signed-in ``access_token`` (scope
        ``substrate.office.com/sydney``) for the account ``oid``, sends one
        ``chat`` invocation and yields reply text as it streams. ``prompt`` is
        sent as one user message (the protocol has no separate system/role
        channel).

        ``tenant`` is the account's AAD tenant, which routes the hub (consumer
        accounts -> MSA tenant, work/school accounts -> the organisation's). It
        defaults to the one captured at sign-in by :class:`copilot.CopilotClient`,
        and otherwise to the active profile's default.

        Conversation targeting (first match wins):
          * ``conversation`` — reuse an existing :class:`Conversation` object;
          * ``conversation_id`` — continue a conversation by its id string;
          * neither — start a fresh one (the client picks its id). With
            ``return_conversation`` the new :class:`Conversation` is yielded first.
        """
        if image is not None:
            raise NotImplementedError("Image input isn't supported on copilot.com yet.")
        if conversation is not None:
            conversation_id = conversation.conversation_id
            access_token = access_token or conversation.access_token
        if not access_token or not oid:
            raise ValueError("A signed-in access_token and account oid are required "
                             "(see copilot.auth.load_auth).")
        tenant = tenant or current_profile().default_tenant
        if not tenant:
            raise ValueError(
                "No hub tenant for this profile. Sign in with the work account so "
                "the tenant is captured (`python -m copilot login`), or set "
                "COPILOT_TENANT=<tenant-id>."
            )

        start_of_session = conversation_id is None
        if start_of_session:
            conversation_id = str(uuid.uuid4())
            if return_conversation:
                yield Conversation(conversation_id, CookieJar(), access_token)

        request_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        websocket_url = build_hub_url(
            oid=oid,
            tenant=tenant,
            conversation_id=conversation_id,
            request_id=request_id,
            session_id=session_id,
            access_token=access_token,
        )

        with Session(timeout=timeout, proxy=proxy, impersonate="chrome",
                     headers={"Origin": current_profile().web_origin}) as session:
            try:
                wss = session.ws_connect(websocket_url)
            except CurlError as exc:
                if "401" in str(exc):
                    raise AuthExpired(f"Copilot rejected the access token: {exc}") from exc
                if "403" in str(exc):
                    raise AuthExpired(
                        "Copilot refused the connection (403): the token may be "
                        "rejected, or the account/tenant isn't licensed for this "
                        f"experience. {exc}"
                    ) from exc
                raise
            wss.send(HANDSHAKE, CurlWsFlag.TEXT)
            wss.send(PING, CurlWsFlag.TEXT)
            wss.send(encode(chat_frame(prompt, request_id, session_id, start_of_session)),
                     CurlWsFlag.TEXT)
            yield from self._read_stream(wss, timeout)

    def _read_stream(self, wss, timeout: int, idle_timeout: int = 90):
        """Consume hub records until the turn completes, yielding reply text.

        ``idle_timeout`` bounds how long we wait for the *next* frame, so a
        stalled socket raises instead of blocking for the full ``timeout``.
        """
        parser = TurnParser()
        buffer = ""
        last_record = None
        overall_deadline = time.time() + timeout
        while not parser.done:
            idle_deadline = time.time() + idle_timeout
            try:
                chunk = self._recv_frame(wss, min(overall_deadline, idle_deadline))
            except Exception:
                break  # socket closed/errored -> end of stream
            if chunk is None:  # deadline passed with no frame
                if time.time() >= overall_deadline:
                    raise TimeoutError(f"Copilot stream exceeded {timeout}s")
                raise TimeoutError(
                    f"Copilot chat socket went silent for {idle_timeout}s; "
                    f"last record was {last_record!r}."
                )
            buffer += chunk.decode("utf-8", "replace")
            *complete, buffer = buffer.split("\x1e")
            for record in split_records("\x1e".join(complete)):
                last_record = record
                for text in parser.feed(record):
                    yield text
                if parser.error:
                    self._raise_error(parser.error)

        if not parser.done and not parser.text:
            raise RuntimeError(f"Invalid response: {last_record}")

    @staticmethod
    def _raise_error(error) -> None:
        message = str(error)
        lowered = message.lower()
        if any(word in lowered for word in ("challenge", "captcha", "turnstile")):
            raise ClearanceRequired(f"Copilot asked for human verification: {message}")
        if any(word in lowered for word in
               ("license", "licence", "entitle", "no seat", "subscription required",
                "upgrade your plan", "not authorized to use")):
            raise LicenseRequired(
                f"Copilot says this account isn't licensed for chat: {message}"
            )
        if any(word in lowered for word in
               ("unauthorized", "token", "authentication", "aadsts",
                "invalid_grant", "tenant")):
            raise AuthExpired(f"Copilot rejected the access token: {message}")
        raise RuntimeError(f"Copilot error: {message}")

    @staticmethod
    def _recv_frame(wss, deadline: float):
        """Block for one complete WS frame, or return ``None`` past ``deadline``.

        Reassembles libcurl's fragments like ``curl_cffi``'s own ``recv()`` but
        breaks out of the ``CURLE_AGAIN`` wait once ``deadline`` (epoch seconds)
        is reached, so an idle socket can't hang us indefinitely. Non-AGAIN curl
        errors (e.g. a closed connection) propagate to the caller.
        """
        sock_fd = wss.curl.getinfo(CurlInfo.ACTIVESOCKET)
        if sock_fd == _CURL_SOCKET_BAD:
            raise ConnectionError("WebSocket has no active socket")
        chunks = []
        while True:
            try:
                chunk, frame = wss.recv_fragment()
                chunks.append(chunk)
                if frame.bytesleft == 0 and frame.flags & CurlWsFlag.CONT == 0:
                    return b"".join(chunks)
            except CurlError as e:
                if e.code != CurlECode.AGAIN:
                    raise
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                select([sock_fd], [], [], min(0.5, remaining))
