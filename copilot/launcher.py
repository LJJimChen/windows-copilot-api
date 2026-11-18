"""Which browser to drive — the one already installed on this machine.

The browser is only ever used to sign in and to capture the chat token (see
:mod:`copilot.browser`); all chatting is the pure-HTTP driver's job. Driving the
*installed* browser instead of Playwright's bundled Chromium has two practical
advantages: there is no separate ~150 MB download, and the sign-in happens in the
same browser (and often with the same corporate policies) the user already uses.

By default the local **Microsoft Edge** is launched through Playwright's
``channel="msedge"``. If Edge can't be started — a slim Docker image, a Linux box
without Edge — the launcher falls back to Playwright's bundled Chromium, which is
what ``playwright install chromium`` provides.

Environment overrides (all optional):

    COPILOT_BROWSER_CHANNEL      msedge (default) | chrome | msedge-beta | chrome-beta
                                 | bundled  (Playwright's own Chromium)
    COPILOT_BROWSER_EXECUTABLE   absolute path to a browser binary; wins over the channel

An explicit channel/executable never falls back: if you asked for Chrome and it
isn't installed, you get an error rather than a silently different browser.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

# Edge is what this project targets, and it ships with Windows/macOS.
DEFAULT_CHANNEL = "msedge"
# Values that mean "don't pick a channel; use Playwright's downloaded Chromium".
_BUNDLED = ("bundled", "playwright", "none", "false", "0")
# Same hardening the driver has always used: hide the automation switch.
_AUTOMATION_ARGS = ["--disable-blink-features=AutomationControlled"]
IGNORED_ARGS = ["--enable-automation"]

_UNSET = object()  # distinguishes "not passed" from "explicitly None"


def _env(name: str) -> Optional[str]:
    value = (os.environ.get(name) or "").strip()
    return value or None


def browser_executable() -> Optional[str]:
    """A specific browser binary from ``COPILOT_BROWSER_EXECUTABLE``, if any."""
    return _env("COPILOT_BROWSER_EXECUTABLE")


def browser_channel() -> Optional[str]:
    """The Playwright channel to use, or ``None`` for the bundled Chromium.

    Defaults to Edge. ``COPILOT_BROWSER_CHANNEL=bundled`` (also ``playwright``,
    ``none``, ``false`` or ``0``) opts out of the branded browser entirely.
    """
    value = _env("COPILOT_BROWSER_CHANNEL")
    if value is None:
        return DEFAULT_CHANNEL
    if value.lower() in _BUNDLED:
        return None
    return value


def _attempts() -> Tuple[List[Dict[str, str]], bool]:
    """Launch options to try, in order, plus whether they came from an override."""
    executable = browser_executable()
    if executable:
        # An explicit binary is a request, not a preference: never substitute.
        return [{"executable_path": executable}], True

    if _env("COPILOT_BROWSER_CHANNEL") is not None:
        channel = browser_channel()
        return ([{"channel": channel}] if channel else [{}]), True

    # Default path: the installed Edge, then Playwright's Chromium as a fallback.
    return [{"channel": DEFAULT_CHANNEL}, {}], False


def _label(options: Dict[str, str]) -> str:
    if options.get("channel"):
        return f"{options['channel']} (installed browser)"
    if options.get("executable_path"):
        return options["executable_path"]
    return "Playwright's bundled Chromium"


def describe() -> str:
    """One-line description of the browser that will be used (no launch)."""
    attempts, custom = _attempts()
    names = [_label(options) for options in attempts]
    if custom:
        return names[0]
    return f"{names[0]}, falling back to {names[1]}"


def launch_persistent_context(
    playwright,
    user_data_dir,
    *,
    headless: bool = False,
    proxy: Optional[dict] = None,
    channel: object = _UNSET,
    executable_path: object = _UNSET,
):
    """Launch a persistent context, preferring the installed Edge.

    Returns ``(context, label)`` so the caller can log which browser actually
    started. Raises :class:`RuntimeError` with every attempt's error attached if
    nothing could be launched.
    """
    if channel is not _UNSET or executable_path is not _UNSET:
        options: Dict[str, str] = {}
        if executable_path is not _UNSET and executable_path:
            options["executable_path"] = str(executable_path)
        elif channel is not _UNSET and channel:
            options["channel"] = str(channel)
        attempts: List[Dict[str, str]] = [options]
    else:
        attempts, _ = _attempts()

    errors: List[str] = []
    for attempt in attempts:
        launch_kwargs: dict = {
            "headless": headless,
            "args": list(_AUTOMATION_ARGS),
            "ignore_default_args": list(IGNORED_ARGS),
            **attempt,
        }
        if proxy:
            launch_kwargs["proxy"] = proxy
        try:
            context = playwright.chromium.launch_persistent_context(
                str(user_data_dir), **launch_kwargs
            )
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            errors.append(f"{_label(attempt)}: {exc}")
            continue
        return context, _label(attempt)

    raise RuntimeError(
        "could not start a browser:\n  " + "\n  ".join(errors)
        + "\n\nInstall Microsoft Edge, or "
        "`playwright install chromium` and set COPILOT_BROWSER_CHANNEL=bundled."
    )
