"""
Copilot API - An unofficial Python wrapper for Microsoft Copilot consumer chat.

Basic usage — one client, conversations addressed by id:

>>> from copilot import CopilotClient
>>> client = CopilotClient()
>>> r = client.chat("Hello!")               # new conversation
>>> r.text, r.conversation_id
>>> client.chat("And again?", r.conversation_id)   # continue it
>>> for chunk in client.stream("Stream this"):     # incremental output
...     print(chunk, end="")
"""

__version__ = '1.0.0'

from pathlib import Path

from dotenv import load_dotenv

# Load `.env` from the project root for the CLI and library use too. The server
# does the same in :mod:`server.config`; loading twice is harmless.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .auth import load_auth
from .browser import BrowserCopilot
from .client import ChatReply, CopilotClient
from .driver import ClearanceRequired, Copilot, LicenseRequired
from .profile import Profile, current_profile

__all__ = [
    'CopilotClient',
    'ChatReply',
    'Copilot',
    'ClearanceRequired',
    'LicenseRequired',
    'BrowserCopilot',
    'load_auth',
    'Profile',
    'current_profile',
]
