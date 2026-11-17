"""Capture what a licensed (Microsoft 365) Copilot client tells the hub.

Run this on the machine where you are signed in with the **work/school** account
that has a Copilot licence. It opens the real web app, watches the chat socket
and the token request, and writes ``session/enterprise_profile.json``.

The file is designed to be safe to share:

* no access tokens, refresh tokens, cookies or auth codes;
* no prompt text and no reply text;
* no account id (the oid is only used to check a boolean, then dropped);

It contains exactly what the bridge cannot derive on its own: the hub's routing
parameters (``licenseType``, ``scenario``, ...), the ``clientInfo`` branding, the
shape of the chat frame, which response records the turn produced, and *which*
AAD tenant the token endpoint was addressed to (plus the public client ids).

Two steps::

    COPILOT_PROFILE=enterprise python -m copilot login    # once, if not already
    COPILOT_PROFILE=enterprise python tests/capture_enterprise.py

Then send ``session/enterprise_profile.json`` back. The script also prints the
``COPILOT_*`` overrides to apply if your tenant differs from the preset.

Manual alternative (if you'd rather not run Playwright): open
https://m365.cloud.microsoft/chat signed in, open DevTools → Network → WS, click
the ``Chathub`` connection, send one short message, then copy:

* the socket **URL** (it carries ``licenseType``, ``scenario``, ``agentHost`` …),
* the first **sent** frame (a JSON record with ``target: "chat"`` — it carries
  ``clientInfo``),
* for the ``/oauth2/v2.0/token`` request: the path's tenant segment, the
  ``Origin`` header, and the form fields ``client_id`` / ``brk_client_id`` /
  ``redirect_uri`` / ``brk_redirect_uri``,

and redact ``access_token`` / ``refresh_token`` / ``code`` before sharing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import Error as PlaywrightError, sync_playwright  # noqa: E402

from copilot.auth import DEFAULT_PROFILE_DIR, DEFAULT_AUTH_FILE  # noqa: E402
from copilot.profile import current_profile  # noqa: E402

OUT = Path("session/enterprise_profile.json")
HUB_HINT = "/m365Copilot/Chathub/"

# Query parameters that are safe to publish: they configure the experience, not
# authenticate it. Everything else (token, per-turn ids) is dropped.
_SAFE_QUERY = ("source", "product", "agentHost", "licenseType", "isEdu", "agent", "scenario")
# Form values that are public Client/Application ids of the SPA and its broker.
_SAFE_FORM = ("client_id", "brk_client_id", "redirect_uri", "brk_redirect_uri",
              "scope", "grant_type", "client_info")
_SAFE_HEADERS = ("origin", "referer", "content-type", "accept", "user-agent")
# clientInfo keys that identify the session rather than the client build.
_DROP_CLIENT_INFO = ("clientSessionId",)
# Upstream error strings occasionally quote an address or an opaque token.
_REDACT = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"[A-Za-z0-9_\-]{40,}"), "<redacted>"),
)


def _redact(text: str) -> str:
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


def records(payload) -> List[dict]:
    """Split one SignalR payload into JSON records (non-JSON parts dropped)."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    out = []
    for part in payload.split("\x1e"):
        part = part.strip()
        if not part:
            continue
        try:
            obj = json.loads(part)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def summarise_hub_url(url: str) -> dict:
    """The routing parameters of a chat socket URL, without secrets."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    safe = {key: (query.get(key) or [""])[0] for key in _SAFE_QUERY if key in query}
    variants = (query.get("variants") or [""])[0]
    # /m365Copilot/Chathub/<oid>@<tenant>
    tenant = ""
    if "@" in parsed.path:
        tenant = parsed.path.rsplit("@", 1)[1]
    return {
        "host": parsed.hostname or "",
        "path_template": HUB_HINT + "<oid>@<tenant>",
        "path_tenant": tenant,
        "query": safe,
        "variants": {
            "length": len(variants),
            "sha256": hashlib.sha256(variants.encode()).hexdigest()[:16] if variants else "",
            "value": variants,
        },
        "other_query_keys": sorted(k for k in query if k not in _SAFE_QUERY and k != "variants"),
    }


def summarise_chat_frame(record: dict) -> Optional[dict]:
    """The parts of a ``chat`` invocation that describe the client, not the user."""
    if record.get("type") != 4 or record.get("target") != "chat":
        return None
    args = record.get("arguments") or []
    if not args or not isinstance(args[0], dict):
        return None
    arg = args[0]
    message = arg.get("message") or {}
    client_info = {k: v for k, v in (arg.get("clientInfo") or {}).items()
                   if k not in _DROP_CLIENT_INFO}
    return {
        "top_level_keys": sorted(record.keys()),
        "argument_keys": sorted(arg.keys()),
        "clientInfo": client_info,
        "message_keys": sorted(message.keys()) if isinstance(message, dict) else [],
        "optionsSets": arg.get("optionsSets") or [],
        "allowedMessageTypes": arg.get("allowedMessageTypes") or [],
        "streamingMode": arg.get("streamingMode"),
        "tone": arg.get("tone"),
        "plugins": arg.get("plugins"),
        "isStartOfSession": arg.get("isStartOfSession"),
        "isSbsSupported": arg.get("isSbsSupported"),
        "disconnectBehavior": arg.get("disconnectBehavior"),
        "threadLevelGptId": arg.get("threadLevelGptId"),
    }


def summarise_token_request(url: str, post_data: str, headers: dict,
                            status: int, body_keys: List[str]) -> dict:
    """The token request's tenant/origin/public ids — never its tokens."""
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    tenant = segments[0] if segments else ""
    form = parse_qs(post_data or "")
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    return {
        "status": status,
        "url_tenant": tenant,
        "form": {key: (form.get(key) or [""])[0] for key in _SAFE_FORM if key in form},
        "form_keys": sorted(form),
        "header_values": {k: lowered[k] for k in _SAFE_HEADERS if k in lowered},
        "header_names": sorted(lowered),
        "response_keys": sorted(body_keys),
    }


def suggest_overrides(captured_hub: Dict[str, str], client_info: Dict[str, str],
                      profile) -> Dict[str, str]:
    """``COPILOT_*`` env vars that make the preset match what was captured."""
    mapping = {
        "source": "COPILOT_SOURCE",
        "product": "COPILOT_PRODUCT",
        "agentHost": "COPILOT_AGENT_HOST",
        "agent": "COPILOT_AGENT",
        "licenseType": "COPILOT_LICENSE_TYPE",
        "scenario": "COPILOT_SCENARIO",
        "isEdu": "COPILOT_IS_EDU",
    }
    out = {}
    for key, env in mapping.items():
        value = captured_hub.get(key)
        if value is not None and value != profile.hub_params.get(key):
            out[env] = value
    platform = client_info.get("clientPlatform")
    if platform and platform != "copilotcom-web":
        out["COPILOT_CLIENT_PLATFORM"] = platform
    entrypoint = client_info.get("clientEntrypoint")
    if entrypoint and entrypoint != "copilotcom-officeweb":
        out["COPILOT_CLIENT_ENTRYPOINT"] = entrypoint
    return out


class Capture:
    """Collects the summary while the real page runs one chat turn."""

    def __init__(self) -> None:
        self.hub: Optional[dict] = None
        self.frame: Optional[dict] = None
        self.token: Optional[dict] = None
        self.frames = {"records": 0, "update": 0, "final_result": 0,
                       "completion": 0, "closed": 0, "saw_writeAtCursor": False,
                       "saw_messages": False, "error_records": []}
        self.done_at: Optional[float] = None

    # -- websocket ---------------------------------------------------------

    def on_websocket(self, ws) -> None:
        if HUB_HINT not in ws.url:
            return
        if self.hub is None:
            self.hub = summarise_hub_url(ws.url)
        ws.on("framesent", self.on_sent)
        ws.on("framereceived", self.on_received)
        ws.on("close", lambda *_: self.frames.__setitem__(
            "closed", self.frames["closed"] + 1))

    def on_sent(self, payload) -> None:
        if self.frame is not None:
            return
        for record in records(payload):
            summary = summarise_chat_frame(record)
            if summary:
                self.frame = summary

    def on_received(self, payload) -> None:
        for record in records(payload):
            self.frames["records"] += 1
            kind = record.get("type")
            if kind == 1 and record.get("target") == "update":
                self.frames["update"] += 1
                for arg in record.get("arguments") or []:
                    if isinstance(arg, dict):
                        if isinstance(arg.get("writeAtCursor"), str) and arg["writeAtCursor"]:
                            self.frames["saw_writeAtCursor"] = True
                        if arg.get("messages"):
                            self.frames["saw_messages"] = True
            elif kind == 2:
                self.frames["final_result"] += 1
                result = ((record.get("item") or {}).get("result") or {})
                if result.get("value") not in (None, "Success"):
                    self.frames["error_records"].append(_redact(str(result.get("value")))[:80])
            elif kind == 3:
                self.frames["completion"] += 1
                if record.get("error"):
                    self.frames["error_records"].append(_redact(str(record["error"]))[:80])
                self.done_at = time.time()
            elif kind == 7:
                self.frames["error_records"].append(
                    _redact(str(record.get("error") or "socket closed"))[:80])
                self.done_at = time.time()

    # -- token endpoint ----------------------------------------------------

    def on_response(self, response) -> None:
        try:
            if "/oauth2/v2.0/token" not in response.url or self.token is not None:
                return
            request = response.request
            try:
                body_keys = sorted(response.json().keys())
            except Exception:
                body_keys = []
            self.token = summarise_token_request(
                response.url, request.post_data or "", dict(request.headers or {}),
                response.status, body_keys,
            )
        except Exception:
            pass  # telemetry response; ignore


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None, help="page to open (default: the profile's chat_url)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for one turn")
    args = parser.parse_args(argv)

    profile = current_profile()
    url = args.url or profile.chat_url
    capture = Capture()

    print(f"profile: {profile.name}")
    print(f"opening: {url}")
    print("Sign in if asked, then send ONE short message (e.g. \"hello\").")
    print("The script finishes by itself once the reply completes.\n")

    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(Path(DEFAULT_PROFILE_DIR).resolve()),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.on("websocket", capture.on_websocket)
            page.on("response", capture.on_response)
            page.goto(url, wait_until="domcontentloaded")

            deadline = time.time() + args.timeout
            while time.time() < deadline:
                if capture.done_at and time.time() - capture.done_at > 3:
                    break
                if page.is_closed():
                    break
                page.wait_for_timeout(500)
            try:
                context.close()
            except Exception:
                pass
    except PlaywrightError as exc:
        print(f"browser failed: {exc}")
        print("Is Chromium installed? Run: playwright install chromium")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; writing whatever was captured")

    if capture.hub is None:
        print("No chat socket was seen — was a message actually sent while signed in?")
        print("Check that you are signed in to the site printed above.")
        return 1

    # A stable ordering makes the file readable and diffable.
    data = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "page_url": url,
        "hub": capture.hub,
        "chat_frame": capture.frame,
        "frames_seen": capture.frames,
        "token_request": capture.token,
        "preset": {
            "profile": profile.name,
            "hub_params": profile.hub_params,
            "web_origin": profile.web_origin,
            "default_tenant": profile.default_tenant,
        },
    }
    overrides = suggest_overrides(
        (capture.hub or {}).get("query") or {},
        ((capture.frame or {}).get("clientInfo") or {}),
        profile,
    )
    data["suggested_overrides"] = overrides

    # Cross-check the hub tenant against the signed-in session, without storing ids.
    try:
        auth = json.loads(Path(DEFAULT_AUTH_FILE).read_text(encoding="utf-8"))
        session_tenant = auth.get("tenant") or (auth.get("anchor") or "").rsplit("@", 1)[-1]
        data["tenant_matches_session"] = bool(
            session_tenant and session_tenant == capture.hub.get("path_tenant"))
    except (OSError, ValueError):
        data["tenant_matches_session"] = None

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {OUT} (no tokens, no message text)")
    print(f"frames seen: {capture.frames}")
    if capture.token:
        print(f"token endpoint tenant: {capture.token['url_tenant']} "
              f"(status {capture.token['status']})")
    print(f"hub tenant matches the signed-in session: {data['tenant_matches_session']}")

    if overrides:
        print("\nApply these if your tenant differs from the preset:")
        for key, value in overrides.items():
            print(f"  export {key}={value}")
    else:
        print("\nCaptured values match the preset — nothing to override.")
    print("\nSend session/enterprise_profile.json back; it is safe to share.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
