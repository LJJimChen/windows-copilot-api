"""Command-line entry point.

    python -m copilot login        # interactive sign-in, persists the session
    python -m copilot ask "hi"     # one-shot completion via the pure-HTTP driver
    python -m copilot doctor       # check profile + session, then probe one turn

Set ``COPILOT_PROFILE=enterprise`` for a Microsoft 365 (work/school) account;
see :mod:`copilot.profile`.
"""

import sys


def _doctor() -> int:
    """Report the active profile and session shape, then send one probe turn."""
    import json
    import time
    from pathlib import Path

    from .auth import DEFAULT_AUTH_FILE
    from .driver import AuthExpired, ClearanceRequired, LicenseRequired
    from .launcher import describe as describe_browser
    from .profile import current_profile, tenant_from_anchor

    profile = current_profile()
    print(f"profile:    {profile.name}")
    print(f"web origin: {profile.web_origin}")
    print(f"chat url:   {profile.chat_url}")
    print("hub params: " + ", ".join(f"{k}={v}" for k, v in profile.hub_params.items()))
    print(f"hub tenant: {profile.default_tenant or '(taken from the signed-in account)'}")
    print(f"browser:    {describe_browser()} (sign-in only)")

    # Read the snapshot directly: reporting must never start a browser.
    path = Path(DEFAULT_AUTH_FILE)
    if not path.exists():
        print(f"session:    MISSING ({path}) — run `python -m copilot login`")
        return 1
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"session:    UNREADABLE ({path}) — {exc}; run `python -m copilot login`")
        return 1

    remaining = int(auth.get("expires_at", 0) - time.time())
    state = "expired" if remaining <= 0 else f"expires in {remaining // 60}m"
    print(
        "session:    oid={oid}… tenant={tenant} {state} refresh token={refresh}".format(
            oid=(auth.get("oid") or "?")[:8],
            tenant=auth.get("tenant") or tenant_from_anchor(auth.get("anchor")) or "?",
            state=state,
            refresh="yes" if auth.get("refresh_token") else "no",
        )
    )

    print("probe:      sending one short message…")
    from .client import CopilotClient

    try:
        reply = CopilotClient(interactive_clear=False).chat("Reply with one word: ok")
    except LicenseRequired as exc:
        print(f"probe:      FAILED (licence) — {exc}")
        print("            -> the tenant must assign a Microsoft 365 Copilot licence")
        return 1
    except AuthExpired as exc:
        print(f"probe:      FAILED (auth) — {exc}")
        print("            -> run `python -m copilot login` again")
        return 1
    except ClearanceRequired as exc:
        print(f"probe:      FAILED (verification) — {exc}")
        print("            -> open the chat site in a browser, pass the check, retry")
        return 1
    except Exception as exc:  # noqa: BLE001 - a doctor reports, it doesn't raise
        print(f"probe:      FAILED — {type(exc).__name__}: {exc}")
        print("            -> run `python tests/diagnostic.py` and check the report")
        return 1

    print(f"probe:      OK — {reply.text.strip()[:80]!r}")
    return 0


def main(argv) -> int:
    cmd = argv[0] if argv else "ask"
    if cmd == "login":
        # The browser is used only for interactive sign-in / token capture.
        from .browser import BrowserCopilot
        from .launcher import describe as describe_browser
        from .profile import current_profile

        profile = current_profile()
        print(f"Signing in for profile '{profile.name}' at {profile.chat_url}")
        print(f"Browser: {describe_browser()}")
        BrowserCopilot(headless=False).login()
        return 0
    if cmd == "doctor":
        return _doctor()
    if cmd == "ask":
        prompt = " ".join(argv[1:]) or "Hello!"
        from .client import CopilotClient

        for chunk in CopilotClient().stream(prompt):
            if isinstance(chunk, str):
                print(chunk, end="", flush=True)
        print()
        return 0
    print(f"Unknown command: {cmd!r}. Use 'login', 'ask <prompt>' or 'doctor'.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
