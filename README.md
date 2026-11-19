

# Windows Copilot API: a free LLM API powered by Microsoft Copilot

![Windows Copilot API — a free, OpenAI-compatible API for your Microsoft Copilot account](assets/windows-copilot-api-banner.png)

**Using your own Microsoft Copilot account.** No API key, no credits, no paid plan: it turns the free chat at [copilot.com](https://copilot.com) (formerly copilot.microsoft.com) into an API you can call from code.

You can use it in two ways:

- 🐍 **As a Python library:** just call `client.chat("Hi")`. Supports streaming and multi-turn conversations.
- 🔌 **As a local OpenAI-compatible API:** runs a server at `http://localhost:8000/v1` that speaks the OpenAI format, so the official `openai` SDK (and any OpenAI-compatible app) works as a drop-in, with `localhost` in place of OpenAI.

You sign in once in a browser with your Microsoft **or Google** account; your session is saved and refreshed automatically after that.

> **Unofficial project.** Not affiliated with or endorsed by Microsoft. It automates the consumer Copilot web experience for personal use, so use it responsibly and within Microsoft's terms.

---

## Table of contents

- [Why use this?](#why-use-this)
- [Requirements](#requirements)
- [Setup (2 minutes)](#setup-2-minutes)
- [Run with Docker (optional)](#run-with-docker-optional)
- [Usage 1: In Python (no server)](#usage-1-in-python-no-server)
- [Usage 2: As an OpenAI-compatible server](#usage-2-as-an-openai-compatible-server)
- [Command line](#command-line)
- [Enterprise accounts (Microsoft 365 Copilot)](#enterprise-accounts-microsoft-365-copilot)
- [Concurrency & stress test](#concurrency--stress-test)
- [Rate limiting](#rate-limiting)
- [Project layout](#project-layout)
- [Notes & limitations](#notes--limitations)
- [Troubleshooting](#troubleshooting)
- [Collaboration & support](#collaboration--support)
- [License](#license)
- [Star History](#star-history)

---

## Why use this?

- **Free:** uses your normal signed-in Copilot, no API billing.
- **Drop-in OpenAI replacement:** point any OpenAI client at `localhost` and it just works.
- **Works everywhere you're signed in:** the signed-in path works even in regions where *anonymous* Copilot is blocked (e.g. India).
- **Streaming + conversations:** token-by-token output and multi-turn threads addressed by `conversation_id`.

---

## Requirements

- **Python 3.9+**
- A **Microsoft account** (the free one you use for Copilot is fine)
- Works on Windows, macOS, and Linux

---

## Setup (2 minutes)

```bash
# 1. Clone the project
git clone <your-repo-url>
cd Windows-Copilot-API
```

**2. Create and activate a virtual environment**

On **macOS / Linux**:

```bash
python3 -m venv venv
source venv/bin/activate
```

On **Windows** (PowerShell):

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

> On Windows you may need to allow script execution once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. In `cmd.exe` activate with `venv\Scripts\activate.bat` instead.

**3. Install dependencies and sign in**

```bash
# Install dependencies
pip install -r requirements.txt

# Sign in once: your installed Microsoft Edge opens, log into your Microsoft account
python -m copilot login
```

The bridge drives the browser **you already have** — Microsoft Edge by default —
so there is nothing extra to download. Playwright's own Chromium is only needed
where Edge isn't available (Docker, minimal Linux, CI):

```bash
playwright install chromium
export COPILOT_BROWSER_CHANNEL=bundled   # use Playwright's Chromium instead of Edge
```

Chrome, Edge Beta and a custom binary are selectable too:
`COPILOT_BROWSER_CHANNEL=chrome` or
`COPILOT_BROWSER_EXECUTABLE="/path/to/browser"`.

The browser **closes by itself** once sign-in is detected — you don't need to press Enter or close it manually. Choose **Yes** if Microsoft asks whether to stay signed in. The steps are logged to `session/login.log` if anything goes wrong. That's it: your token is saved under `session/` (git-ignored, never shared) and reused on every run. **After this, no browser is used:** chatting and token renewal are plain HTTP.

> 🛠️ **Run into trouble during setup or your first run?** Head to the [Troubleshooting](#troubleshooting) section; the bundled diagnostic writes a shareable report.

---

## Run with Docker (optional)

Prefer a container? You can run the OpenAI-compatible server in Docker once you've signed in.

> **Sign in on the host first.** The login step above opens a *visible* browser, which can't run inside the headless container — so run `python -m copilot login` on your host to populate `session/`. The container mounts that folder and renews the chat token over plain HTTP. If the refresh token itself expires, re-run `python -m copilot login` on the host.

```bash
docker compose up --build
# -> Copilot OpenAI-compatible API on http://localhost:8000
```

The [docker-compose.yml](docker-compose.yml) maps port `8000` and bind-mounts your `session/` so the login persists across restarts. Tune `RATE_LIMIT_RPM` / `RATE_LIMIT_BURST` there, and uncomment `COPILOT_PROFILE` for a work/school account. To run without Compose, build and pass the same bindings by hand:

```bash
docker build -t windows-copilot-api .
docker run --rm -p 8000:8000 -v "$(pwd)/session:/app/session" windows-copilot-api
```

---

## Usage 1: In Python (no server)

The simplest way if your code is already Python.

```python
from copilot import CopilotClient

client = CopilotClient()                 # loads your signed-in session

# Get a full reply
reply = client.chat("Say hello in one short sentence.")
print(reply.text)

# Continue the SAME conversation — pass the id back
reply2 = client.chat("And now in French?", reply.conversation_id)
print(reply2.text)

# Stream the answer as it's typed
for chunk in client.stream("Tell me a short joke"):
    print(chunk, end="", flush=True)
```

`chat()` returns the full text plus a `conversation_id`; pass that id back to keep the thread going, or omit it to start fresh. `stream()` yields the reply piece by piece.

👉 More: [examples/01_direct_chat.py](examples/01_direct_chat.py), [02_direct_conversation.py](examples/02_direct_conversation.py), [03_direct_stream.py](examples/03_direct_stream.py)

---

## Usage 2: As an OpenAI-compatible server

Start a local server that speaks the OpenAI API, so existing OpenAI tools and SDKs work unchanged.

```bash
python app.py
# -> Copilot OpenAI-compatible API on http://127.0.0.1:8000
```

Then point any OpenAI client at it (the API key is required by the SDK but ignored):

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

resp = client.chat.completions.create(
    model="copilot",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Or call it with plain HTTP / `curl`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

**Endpoints**

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat: OpenAI `messages`, `tools` / `tool_choice`, `"stream": true`, optional `"conversation_id"` |
| `GET`  | `/v1/models` | Lists the single `copilot` model |

> Change the address with env vars: `HOST=0.0.0.0 PORT=8080 python app.py`, or run `uvicorn server.api:app --host 0.0.0.0 --port 8080`.

👉 More: [examples/04_server_http.py](examples/04_server_http.py), [05_server_stream.py](examples/05_server_stream.py), [06_server_openai_sdk.py](examples/06_server_openai_sdk.py), [07_server_tool_calls.py](examples/07_server_tool_calls.py)

### Tool calling (emulated)

Copilot's consumer protocol answers with plain text only — there is no native
function-calling channel. The server bridges that gap the same way local-model
stacks do: it injects the tool schemas into the prompt, asks for a fenced
```tool_call block, and parses it back into OpenAI `tool_calls`:

```python
resp = client.chat.completions.create(
    model="copilot",
    messages=[{"role": "user", "content": "Weather in Oslo?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }],
    tool_choice="auto",  # "auto" | "none" | "required" | {"type": "function", ...}
)
```

From the client's side this is an ordinary function-calling loop
([examples/07_server_tool_calls.py](examples/07_server_tool_calls.py)): the
response has `finish_reason: "tool_calls"` and `message.tool_calls`; send the
results back as `role: "tool"` messages and continue.

The model is told, in the injected instructions, that it has **no filesystem,
shell, sandbox, canvas or code interpreter** and must not do the work itself —
so a request like "write index.html" produces a tool call instead of Copilot
rendering the file in its own cloud container. Its reply must be one bare JSON
object (no fences, no prose):

```json
{"tool_calls":[{"name":"get_weather","arguments":{"city":"Oslo"}}]}
```

The parser also accepts the shapes other open-source stacks produce
(`<tool_call>`, `[TOOL_CALLS]`, `<|tool_call|>`, DeepSeek blocks, fenced blocks),
so a model that drifts from the requested format still works. Guardrails:

- calls naming tools that weren't declared are **dropped** and returned as text;
- `tool_choice: "required"` gets **one** corrective retry;
- `parallel_tool_calls: false` keeps only the first call;
- `tool_choice: "none"` injects no tools at all;
- streamed tool calls arrive once the turn is complete (arguments are not
  token-streamed), which every OpenAI-compatible client accepts.

Coding agents such as [pi](https://pi.dev) work through their normal
OpenAI-compatible provider config — no extra fields needed:

```json
{
  "providers": {
    "copilot": {
      "baseUrl": "http://127.0.0.1:8000/v1",
      "api": "openai-completions",
      "apiKey": "unused",
      "compat": {
        "supportsUsageInStreaming": false,
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false
      },
      "models": [{ "id": "copilot", "input": ["text"], "contextWindow": 128000, "maxTokens": 8192 }]
    }
  }
}
```

### Conversation reuse

OpenAI clients resend the whole conversation on every call; Copilot threads are
stateful. The server fingerprints the resent history and, when it matches a
thread it already knows, sends **only the new tail** (a user message or the tool
results) instead of the whole conversation again. It works with any client — no
extra field or header needed — and when the client edits, trims or compacts its
history the fingerprint stops matching and a fresh thread is started
automatically.

That cache is in memory, per process, and bounded:

| Env var | Default | Meaning |
| --- | --- | --- |
| `SESSION_TTL` | `1800` | seconds an idle conversation stays reusable |
| `SESSION_MAX` | `64` | max conversations tracked (LRU) |
| `SESSION_REGENERATE_WINDOW` | `300` | identical history within this window counts as a retry |

The explicit `conversation_id` field still works and is used when no history
match is found; on the first turn with it the whole history is resent (legacy
behaviour).


---

## Command line

```bash
python -m copilot login          # sign in and save the session
python -m copilot ask "Hello!"   # quick one-shot question
python -m copilot doctor         # profile + session report, then one probe turn
```

`doctor` never opens a window: it reads the saved session, prints the active
profile, tenant and token expiry, then sends a single message and reports what
failed (sign-in, licence, verification) if anything did.

---

## How the session works

Copilot moved from copilot.microsoft.com to **copilot.com**, which runs on the
Microsoft 365 Copilot backend. The bridge talks to it directly over HTTP — a
WebSocket to `substrate.office.com` (see [copilot/protocol.py](copilot/protocol.py)) —
authenticated with your signed-in chat token:

- **At sign-in:** `python -m copilot login` captures the chat token (valid ~8
  hours) and a refresh token into `session/token.json`.
- **When the token expires:** it's renewed over plain HTTP with the refresh
  token, transparently, in well under a second.
- **If the refresh token expires too:** the bridge re-mints it once from your
  signed-in browser profile, headlessly; if that profile is signed out, run
  `python -m copilot login` again. The **server** never opens a window: it
  returns an error telling you to sign in.

---

## Enterprise accounts (Microsoft 365 Copilot)

The same backend serves personal accounts (`copilot.com`) and work/school
accounts (`m365.cloud.microsoft`); only the tenant and the routing parameters
differ. Select the licensed front-end with one environment variable:

```bash
COPILOT_PROFILE=enterprise python -m copilot login   # sign in with the work account
COPILOT_PROFILE=enterprise python app.py             # serve it
```

The account's AAD tenant is captured at sign-in and used from then on — for the
chat socket and for token refresh — so nothing else has to be configured. Check
the whole chain with one command:

```bash
COPILOT_PROFILE=enterprise python -m copilot doctor
```

It prints the profile, the effective hub parameters, the account's tenant and
token expiry, then sends a single probe turn. A missing licence, an expired
token and a verification challenge are reported separately, each with the next
step.

**What the profile changes** (all overridable, in case Microsoft moves a value):

| Env var | Default (consumer) | Enterprise preset |
| --- | --- | --- |
| `COPILOT_PROFILE` | `consumer` | `enterprise` |
| `COPILOT_CHAT_URL` | `https://copilot.com/chat` | `https://m365.cloud.microsoft/chat` |
| `COPILOT_WEB_ORIGIN` | `https://copilot.com` | `https://m365.cloud.microsoft` |
| `COPILOT_LICENSE_TYPE` | `Free` | `Copilot` |
| `COPILOT_SCENARIO` | `OfficeWebFreeConsumerCopilot` | `OfficeWebCopilot` |
| `COPILOT_TENANT` | MSA tenant | the signed-in account's tenant |
| `COPILOT_SOURCE` / `_PRODUCT` / `_AGENT_HOST` / `_AGENT` / `_IS_EDU` | same on both | same on both |
| `COPILOT_CLIENT_PLATFORM` / `_CLIENT_ENTRYPOINT` | `copilotcom-web` / `copilotcom-officeweb` | same on both |
| `COPILOT_BROKER_CLIENT_ID` / `_BROKER_REDIRECT_URI` | copilot.com's MSAL broker | same on both |
| `COPILOT_LOGIN_HOSTS` | — | extra sign-in hosts (ADFS, Okta, …) |

Things to know before relying on it:

- **A licence is required.** Microsoft 365 Copilot is a paid per-user add-on; a
  tenant without it answers with a licence error, which the API surfaces as
  `403 license_required` instead of a generic `502`.
- **Conditional Access can shorten the session.** MFA, sign-in-frequency and
  compliant-device policies may stop the headless re-mint from working, so plan
  on `python -m copilot login` more often. The server still never opens a window.
- **Unverified fields.** `licenseType`/`scenario` (and the two `clientInfo`
  branding fields) were the only values that could not be derived from the
  consumer capture. Capture them from your own tenant with the script below, or
  set them by hand.
- Please keep the rate limit sane: a work account is tied to a tenant, and
  administrators can see usage.

### Capturing the licensed values

The preset was inferred; your tenant is the ground truth. One command records
what the real web client sends, then prints the overrides to apply:

```bash
COPILOT_PROFILE=enterprise python -m copilot login     # once, if not already
COPILOT_PROFILE=enterprise python tests/capture_enterprise.py
```

It opens `https://m365.cloud.microsoft/chat`, waits for you to send **one short
message**, and writes `session/enterprise_profile.json`. The file contains the
hub's routing parameters, `clientInfo`, the shape of the chat frame, which
response records the turn produced, and which AAD tenant the token endpoint was
addressed to. It contains **no tokens, no cookies, no message text and no
account id** — a test asserts the redaction — so it is safe to attach to an
issue. If the captured values differ from the preset, the script prints the
matching `COPILOT_*` exports.

Prefer doing it by hand? Open the site signed in, then DevTools → Network → WS:

1. click the `Chathub` connection and copy its **URL** (carries `licenseType`,
   `scenario`, `agentHost`, `source`, `variants`);
2. send a message and copy the first **sent** frame (JSON with
   `"target": "chat"`; `clientInfo` is inside `arguments[0]`);
3. for the `/oauth2/v2.0/token` request, note the **tenant segment in the path**,
   the `Origin` header, and the form values `client_id` / `brk_client_id` /
   `redirect_uri` / `brk_redirect_uri`.

Redact `access_token`, `refresh_token` and `code` before sharing anything.

---

## Concurrency & stress test

The server bridges a **single** signed-in Copilot account, and Copilot's chat
socket doesn't tolerate concurrent conversations from one process. So the server
**serializes** upstream calls: parallel HTTP requests queue behind a lock and run
one at a time (see [server/api.py](server/api.py)). This is intentional, and it
means throughput is sequential, not parallel.

You can measure where it breaks with the included stress test, which fires a
batch of simultaneous requests and **doubles the batch size every successful
round** until the first error:

```bash
# Start the server in one terminal
python app.py

# Ramp concurrency in another (1 → 2 → 4 → 8 → …)
python tests/stress.py
python tests/stress.py --max 64 --timeout 120 --url http://localhost:8000
```

**Sample run** (one signed-in account):

| Concurrency | Result | Wall time | Latency (min / median / max) |
| --- | --- | --- | --- |
| 1 | ✓ all ok | 3.7s | 3.7 / 3.7 / 3.7s |
| 2 | ✓ all ok | 4.6s | 3.4 / 4.6 / 4.6s |
| 4 | ✓ all ok | 8.3s | 3.7 / 6.7 / 8.3s |
| 8 | ✗ 1 failed (`HTTP 502`) | 13.3s | 3.5 / 9.7 / 13.3s |

**Highest fully-successful concurrency: 4.** Wall time roughly doubles each round
while *minimum* latency stays flat (~3.5s) — the signature of a serialized queue:
one request runs immediately, the rest wait their turn. The failure at 8 is an
upstream `502` (Copilot rejecting requests under load), not a server crash or
timeout — so the exact break point is flaky and may vary between runs.

> Takeaway: keep concurrent in-flight requests low (≈ 1–4). This is a personal
> bridge, not a high-throughput gateway — and please don't hammer your account.

---

## Rate limiting

Concurrency (above) is *how many at once*; the **rate limit** is *how many per
minute, sustained*. Microsoft publishes none for consumer Copilot, so the bridge
enforces a self-imposed one with a [token bucket](server/ratelimit.py): it caps
accepted requests per minute and returns a standard `429` + `Retry-After` when
you exceed it. Two env vars tune it:

| Env var | Default | Meaning |
| --- | --- | --- |
| `RATE_LIMIT_RPM` | `12` | Requests/minute the bridge accepts. `0` disables the limit. |
| `RATE_LIMIT_BURST` | `4` | How many requests may go back-to-back before pacing kicks in. |

```bash
RATE_LIMIT_RPM=20 RATE_LIMIT_BURST=5 python app.py   # raise it; 0 to disable
```

The default 12 rpm sits safely below the ~15 rpm where a single account starts
seeing upstream `502`s. To find *your* ceiling, run the server with the limiter
off (`RATE_LIMIT_RPM=0`) and push the probe until failures appear:

```bash
python tests/ratelimit.py --rpm 20 --minutes 3
```

**On the client side, use exponential backoff.** Both `429` (bridge limit) and
the occasional `502` (Copilot upstream hiccup) are transient — retry with
growing delays (e.g. 1s, 2s, 4s) and they almost always clear. The official
`openai` SDK does this automatically and honours `Retry-After`; with plain HTTP,
add a few retries yourself.

---

## Project layout

| Path | What it does |
| --- | --- |
| [copilot/](copilot/) | The core library: `CopilotClient`, auth + token refresh, browser sign-in ([copilot/launcher.py](copilot/launcher.py) picks your installed Edge), HTTP driver, protocol, and the consumer/enterprise profiles ([copilot/profile.py](copilot/profile.py)) |
| [server/](server/) | The FastAPI OpenAI-compatible server |
| [examples/](examples/) | Runnable examples for every feature ([examples/README.md](examples/README.md)) |
| [tests/](tests/) | Test scripts: the concurrency stress test ([tests/stress.py](tests/stress.py)), the diagnostic & report tool ([tests/diagnostic.py](tests/diagnostic.py)), the enterprise capture helper ([tests/capture_enterprise.py](tests/capture_enterprise.py)), and the unit tests |
| [app.py](app.py) | Starts the server |

---

## Notes & limitations

- **Sign in once, then reuse.** The cached token refreshes automatically over HTTP; you only re-sign-in if the session fully expires.
- **Daily allowance.** The free copilot.com tier meters usage per day (the reply metadata shows a daily quota that resets at midnight UTC). Use it in moderation, and don't hammer it with automated bulk requests.
- **Text only for now.** Image input isn't supported on the new copilot.com backend yet.
- **One model.** Copilot has no model picker, so the server advertises a single model named `copilot`.
- **Roughly GPT-4 class.** On GPQA Diamond (198 graduate-level questions, closed-book) it scores **40.9%**, which puts it in the GPT-4 family rather than the reasoning tier (o1/o3). Measured with [tests/gpqa_bench.py](tests/gpqa_bench.py).
- **The browser is only for sign-in, and it's yours.** `python -m copilot login`
  drives the installed Microsoft Edge (override with `COPILOT_BROWSER_CHANNEL`,
  or point `COPILOT_BROWSER_EXECUTABLE` at any Chromium-family binary). Chatting
  and token renewal stay pure HTTP.
- **Your session is private.** Everything in `session/` (browser profile + tokens) stays on your machine and is git-ignored.

---

## Troubleshooting

If sign-in or requests fail, first re-run `python -m copilot login`. If that
doesn't help, run the diagnostic — it writes a shareable report.

```bash
python tests/diagnostic.py                # browser capture + report
python tests/diagnostic.py --report-only  # headless/VPS: report only, no browser
```

The default run opens a browser on the same `session/profile/` the bridge uses
and asks you to send one short message; it captures the chat protocol to
`session/ws_capture.log` (a clean turn shows `update` frames with
`writeAtCursor` text, then a `"type":3` completion). That's what's needed to
follow a protocol change on Microsoft's side.

It also writes `session/diagnostic_report.txt` — environment, the *shape* of your
session (token presence/length/expiry, never the values), a live chat probe, and
redacted log tails. Access tokens, cookies, OAuth codes, and emails are redacted
before anything is written, but your message and the reply appear as plain text,
so skim it before attaching it to a GitHub issue.

---

## Collaboration & support

Need a hand getting this running? Open a [GitHub issue](../../issues) for bugs (for setup/auth problems, attach the redacted `diagnostic_report.txt` from `python tests/diagnostic.py`), start a [discussion](../../discussions) to share ideas, or send a pull request.

And if you're working on something interesting, or looking for someone to build it, I'm always open to a chat. Feel free to reach out:

- X: [@sums001](https://x.com/sums001)
- Email: [devsum0101@gmail.com](mailto:devsum0101@gmail.com)
- Discord: `sum_s_s`

---

## License

Released under the [MIT License](LICENSE). As this is an unofficial project, you remain responsible for complying with Microsoft's terms of service.

---

## Star History

<a href="https://www.star-history.com/?repos=sums001%2FWindows-Copilot-API&type=timeline&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=sums001/Windows-Copilot-API&type=timeline&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=sums001/Windows-Copilot-API&type=timeline&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=sums001/Windows-Copilot-API&type=timeline&legend=top-left" />
 </picture>
</a>
