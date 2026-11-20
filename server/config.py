"""Server configuration — shared constants."""

import os
from pathlib import Path

from dotenv import load_dotenv

# `.env` in the project root is loaded on import. Real environment variables win
# over the file, and a missing file is fine.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# The single model id this bridge advertises (Copilot has no model selector).
MODEL_NAME = "copilot"

# Self-imposed rate limit (Copilot publishes none). Tune to whatever ceiling the
# probe in tests/ratelimit.py shows your account tolerates.
#   RATE_LIMIT_RPM   requests/minute the bridge will accept; 0 disables limiting.
#   RATE_LIMIT_BURST max requests allowed back-to-back before pacing kicks in.
# Default 12 rpm sits safely below the ~15 rpm where one account starts seeing
# upstream 502s, so the limiter only bites when callers try to exceed that.
RATE_LIMIT_RPM = float(os.environ.get("RATE_LIMIT_RPM", "12"))  # 12 rpm ≈ 5s per call
RATE_LIMIT_BURST = int(os.environ.get("RATE_LIMIT_BURST", "4"))

# Conversation reuse. Clients resend full history on every call (OpenAI's API is
# stateless); the bridge fingerprints that history and continues the matching
# Copilot thread, sending only the new tail. Small in-memory cache, tuned for
# personal use:
#   SESSION_TTL                seconds an idle conversation stays reusable
#   SESSION_MAX                max conversations tracked (LRU eviction)
#   SESSION_REGENERATE_WINDOW  how recently an identical history must have been
#                              used to count as a retry rather than a new chat
SESSION_TTL = float(os.environ.get("SESSION_TTL", "1800"))
SESSION_MAX = int(os.environ.get("SESSION_MAX", "64"))
SESSION_REGENERATE_WINDOW = float(os.environ.get("SESSION_REGENERATE_WINDOW", "300"))

# Prompt diagnostics. A long prompt can *look* cut off in Copilot's web UI even
# though the whole string was sent, so LOG_PROMPT prints the exact payload (and
# its size) right before each upstream send.
#   LOG_PROMPT  1/true/yes/on prints every prompt to stderr.
LOG_PROMPT = os.environ.get("LOG_PROMPT", "").strip().lower() in (
    "1", "true", "yes", "on"
)
