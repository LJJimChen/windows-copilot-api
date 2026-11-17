"""Runtime profiles: consumer Copilot vs Microsoft 365 (work) Copilot.

Both front-ends drive the *same* chat backend — a SignalR hub at
``wss://substrate.office.com/m365Copilot/Chathub/<oid>@<tenant>`` — so the
transport in :mod:`copilot.driver` is shared. What differs is only what the hub
is told about the caller, and where the session's tokens come from:

* **the tenant in the hub URL** — Microsoft accounts are routed through the MSA
  tenant, work/school accounts through their organisation's tenant;
* **the routing parameters on the socket** — ``licenseType`` and ``scenario``
  select the consumer vs the licensed Copilot experience;
* **the web origin** — refresh tokens are origin-bound, so they must be redeemed
  with the ``Origin`` of the page that minted them (copilot.com vs
  m365.cloud.microsoft).

Pick a profile with ``COPILOT_PROFILE``::

    COPILOT_PROFILE=consumer     # default: copilot.com, personal Microsoft account
    COPILOT_PROFILE=enterprise   # m365.cloud.microsoft, Microsoft 365 Copilot seat

Every field can also be overridden individually, so when Microsoft shifts a
value you can correct it without touching code:

    COPILOT_TENANT                 hub tenant (usually taken from the account)
    COPILOT_WEB_ORIGIN             Origin used when refreshing the token
    COPILOT_CHAT_URL               page opened for sign-in / token capture
    COPILOT_LICENSE_TYPE           hub `licenseType`
    COPILOT_SCENARIO               hub `scenario`
    COPILOT_SOURCE / _PRODUCT / _AGENT_HOST / _AGENT / _IS_EDU
    COPILOT_CLIENT_PLATFORM / _CLIENT_ENTRYPOINT   clientInfo branding
    COPILOT_BROKER_CLIENT_ID / _BROKER_REDIRECT_URI
    COPILOT_LOGIN_HOSTS            extra sign-in hosts, comma-separated

The consumer profile reproduces the previous behaviour byte-for-byte; the
enterprise preset only changes the fields that are known to differ. Anything the
hub rejects with an unfamiliar error should be captured on
``https://m365.cloud.microsoft/chat`` with ``tests/diagnostic.py`` and then
overridden above — see README, "Enterprise accounts".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Dict, Optional

# --- well-known ids ----------------------------------------------------------

# Consumer (Microsoft account) users are routed through the MSA tenant.
MSA_TENANT = "84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"
# The AAD endpoint that mints tokens for Microsoft accounts ("consumers").
CONSUMERS_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"

# copilot.com's MSAL broker, replayed when refreshing the chat token.
BROKER_CLIENT_ID = "4765445b-32c6-49b0-83e6-1d93765276ca"
BROKER_REDIRECT_URI = "https://copilot.com/spalanding"


@dataclass(frozen=True)
class Profile:
    """One deployment target: which site, which tenant, which hub parameters."""

    name: str
    # Origin of the web client: the refresh request must carry it, and the
    # browser uses `chat_url` (same site) for sign-in and token capture.
    web_origin: str
    chat_url: str
    # Query-string parameters that tell the hub which experience to serve.
    hub_params: Dict[str, str]
    # Hub tenant to use when the signed-in account doesn't supply one.
    default_tenant: Optional[str]
    broker_client_id: str
    broker_redirect_uri: str


_CONSUMER = Profile(
    name="consumer",
    web_origin="https://copilot.com",
    chat_url="https://copilot.com/chat",
    hub_params={
        "source": '"officeweb"',
        "product": "Office",
        "agentHost": "Bizchat.FullScreen",
        "licenseType": "Free",
        "isEdu": "false",
        "agent": "web",
        "scenario": "OfficeWebFreeConsumerCopilot",
    },
    default_tenant=MSA_TENANT,
    broker_client_id=BROKER_CLIENT_ID,
    broker_redirect_uri=BROKER_REDIRECT_URI,
)

# Microsoft 365 Copilot (work/school) web app. Same hub, licensed experience.
# `licenseType` / `scenario` are the two values that select it; if Microsoft
# renames them, capture a turn on m365.cloud.microsoft/chat and set
# COPILOT_LICENSE_TYPE / COPILOT_SCENARIO accordingly (no code change needed).
_ENTERPRISE = Profile(
    name="enterprise",
    web_origin="https://m365.cloud.microsoft",
    chat_url="https://m365.cloud.microsoft/chat",
    hub_params={
        "source": '"officeweb"',
        "product": "Office",
        "agentHost": "Bizchat.FullScreen",
        "licenseType": "Copilot",
        "isEdu": "false",
        "agent": "web",
        "scenario": "OfficeWebCopilot",
    },
    # Work accounts carry their own tenant in the sign-in anchor; leave the
    # hard-coded fallback empty so a missing tenant fails loudly instead of
    # quietly talking to the consumer tenant.
    default_tenant=None,
    broker_client_id=BROKER_CLIENT_ID,
    broker_redirect_uri=BROKER_REDIRECT_URI,
)

_PROFILES = {"consumer": _CONSUMER, "enterprise": _ENTERPRISE}
_ALIASES = {
    "consumer": "consumer", "personal": "consumer", "msa": "consumer",
    "enterprise": "enterprise", "work": "enterprise", "school": "enterprise",
    "m365": "enterprise",
}

# env var -> hub_params key
_ENV_HUB_FIELDS = {
    "COPILOT_SOURCE": "source",
    "COPILOT_PRODUCT": "product",
    "COPILOT_AGENT_HOST": "agentHost",
    "COPILOT_AGENT": "agent",
    "COPILOT_LICENSE_TYPE": "licenseType",
    "COPILOT_SCENARIO": "scenario",
    "COPILOT_IS_EDU": "isEdu",
}

# env var -> Profile field
_ENV_FIELDS = {
    "COPILOT_WEB_ORIGIN": "web_origin",
    "COPILOT_CHAT_URL": "chat_url",
    "COPILOT_TENANT": "default_tenant",
    "COPILOT_BROKER_CLIENT_ID": "broker_client_id",
    "COPILOT_BROKER_REDIRECT_URI": "broker_redirect_uri",
}


def current_profile() -> Profile:
    """The active profile, with any ``COPILOT_*`` overrides applied.

    Read on every call (it is cheap and env-driven) so tests and long-running
    processes can change it without reloading modules.
    """
    name = (os.environ.get("COPILOT_PROFILE") or "consumer").strip().lower()
    base = _PROFILES[_ALIASES.get(name, "consumer")]

    hub_params = dict(base.hub_params)
    for env_name, key in _ENV_HUB_FIELDS.items():
        value = os.environ.get(env_name)
        if value:
            hub_params[key] = value

    overrides: Dict[str, object] = {"hub_params": hub_params}
    for env_name, field in _ENV_FIELDS.items():
        value = os.environ.get(env_name)
        if value:
            overrides[field] = value
    if "web_origin" in overrides:
        overrides["web_origin"] = str(overrides["web_origin"]).rstrip("/")
    return replace(base, **overrides)


def tenant_from_anchor(anchor: Optional[str]) -> Optional[str]:
    """Extract the tenant from an ``X-AnchorMailbox`` value (``Oid:<oid>@<tenant>``)."""
    if not anchor or "@" not in anchor:
        return None
    tenant = anchor.rsplit("@", 1)[1].strip().strip("/")
    return tenant or None


def token_tenant_for(tenant: Optional[str]) -> str:
    """The AAD tenant the token endpoint is addressed to.

    Personal accounts mint against ``consumers`` even though their hub tenant is
    the MSA tenant; work accounts mint against their own tenant.
    """
    if not tenant or tenant == MSA_TENANT:
        return CONSUMERS_TENANT
    return tenant


def login_hosts() -> tuple:
    """Hosts treated as "the user is still signing in" by the browser module."""
    extra = os.environ.get("COPILOT_LOGIN_HOSTS", "")
    return tuple(h.strip().lower() for h in extra.split(",") if h.strip())
