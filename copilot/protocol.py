"""Shared Microsoft Copilot chat-protocol constants — the single source of truth.

copilot.microsoft.com now redirects to **copilot.com**, which runs on the
Microsoft 365 Copilot stack. Chat is a SignalR (JSON hub protocol) WebSocket at
``wss://substrate.office.com/m365Copilot/Chathub/<oid>@<tenant>``, authenticated
by an MSAL access token for ``substrate.office.com/sydney`` in the
``access_token`` query parameter. Records are JSON terminated by ``\\x1e``:

    -> {"protocol":"json","version":1}                     handshake
    <- {}
    -> {"type":6}                                          ping
    -> {"type":4,"target":"chat","invocationId":"0","arguments":[CHAT_ARGUMENT]}
    <- {"type":1,"target":"update","arguments":[{"messages":[...]}]}      first chunk
    <- {"type":1,"target":"update","arguments":[{"writeAtCursor":"..."}]} text deltas
    <- {"type":2,"invocationId":"0","item":{...,"result":{"value":"Success"}}}
    <- {"type":3,"invocationId":"0"}                        completion

The conversation id is chosen by the client (a fresh UUID); continuing a
conversation just reuses it. Captured from a live copilot.com session with
``tests/diagnostic.py`` (``session/ws_capture.log``); when Microsoft changes the
protocol, recapture it and update this file.

The same hub serves consumer and Microsoft 365 (work/school) accounts; only the
tenant in the URL and the routing parameters differ, and both come from
:mod:`copilot.profile`.
"""

import json
import os
import time
from datetime import datetime
from typing import List, Optional, Union
from urllib.parse import quote, urlencode

# Re-exported so existing imports (`from copilot.protocol import MSA_TENANT`)
# keep working; the constants themselves now live with the profiles.
from .profile import BROKER_CLIENT_ID, BROKER_REDIRECT_URI, MSA_TENANT

__all__ = [
    "BROKER_CLIENT_ID", "BROKER_REDIRECT_URI", "MSA_TENANT",
    "CHATHUB_URL", "RECORD_SEPARATOR", "CHAT_CLIENT_ID", "CHAT_REDIRECT_URI",
    "CHAT_SCOPE", "HUB_PARAMS", "VARIANTS", "OPTIONS_SETS", "ALLOWED_MESSAGE_TYPES",
    "chat_frame", "build_hub_url", "encode", "split_records", "TurnParser",
    "HANDSHAKE", "PING",
]

CHATHUB_URL = "wss://substrate.office.com/m365Copilot/Chathub/{oid}@{tenant}"
RECORD_SEPARATOR = "\x1e"

# --- auth: the chat token is minted through copilot.com's MSAL broker ---------
# The token *endpoint* is assembled per account in :mod:`copilot.auth` (personal
# accounts use the `consumers` tenant, work accounts their own), and the refresh
# `Origin` / broker ids come from :mod:`copilot.profile`.
CHAT_CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
CHAT_REDIRECT_URI = "brk-multihub://outlook.office.com"
CHAT_SCOPE = "https://substrate.office.com/sydney/v2/.default openid profile offline_access"

# --- chat socket query string (consumer defaults; profiles override) ----------
HUB_PARAMS = {
    "source": '"officeweb"',
    "product": "Office",
    "agentHost": "Bizchat.FullScreen",
    "licenseType": "Free",
    "isEdu": "false",
    "agent": "web",
    "scenario": "OfficeWebFreeConsumerCopilot",
}

# Feature flags the web client sends on the socket URL, replayed verbatim.
VARIANTS = (
    "EnableMcpServerWidgets,feature.EnableMcpServerWidgets,feature.EnableImageGenSystemCapacityThrottled,"
    "feature.EnableLuForChatCIQ,feature.enableChatCIQPlugin,EnableRequestPlugins,feature.EnableSensitivityLabels,"
    "feature.EnableCloudflareTurnstileValidation,EnableUnsupportedUrlDetector,feature.IsCustomEngineCopilotEnabled,"
    "feature.bizchatfluxv3,feature.enablechatpages,feature.enableCodeCanvas,feature.turnOnWorkTabRecommendation,"
    "feature.turnOnDARecommendation,feature.IsStreamingModeInChatRequestEnabled,IncludeSourceAttributionsConcise,"
    "SkipPublishEmptyMessage,feature.EnableDeduplicatingSourceAttributions,feature.IsCitationsReferencesOutputEnabled,"
    "feature.enableDeltaStreamingForReferences,feature.enableIncludeReferencesInDeltaResponse,"
    "feature.enablereferencesforagents,feature.EnableCodeInterpreterConversion,cdxcipreviewmsg,"
    "Enable3PActionProgressMessages,feature.enableClientWebRtc,feature.cwcfluxv3fe,feature.cwcfluxv3fem,"
    "feature.EnableReferencesListCompleteSignal,feature.analyst_agent_parity,feature.StorageMessageSplitDisabled,"
    "SingletonEnvOn,cdxenablefccinmainline,cdxenablefccinresearcher,EnableComposeWidget,"
    "feature.EnableResearcherTodoListObserver,feature.EnableResearcherTodoObserverSlim,feature.EnableResearchSteering,"
    "feature.EnableResearcherTodoSummarizerPacing,agt_researcheragent_enableMemoryRead,"
    "cdxweb_search_citations_answer_cards,agt_bizchat_enableEmailListSdlCard,agt_bizchat_enableEmailHeroSdlCard,"
    "cdxnews_sdl_card,agt_bizchat_enableImageListSdlCard,agt_webagent_enableImageListSdlCard,cdximage_sdl_card,"
    "agt_bizchat_enableMeetingListSdlCard,agt_bizchat_enableMeetingHeroSdlCard,cdxshopping_berry_answer_card_msa,"
    "enable_sdl_widgets,feature.EnableShoppingCashback,agt_module_enableModelDrivenUX,"
    "agt_webagent_enableGenUIHtmlRendering,agt_module_skipGenuiRunIfNoToolWidgets,"
    "unitab_msa_enableEducationLearningTool,skds_msa_EnableGlobalIndex,feature.EnableConversationActions,"
    "feature.cwcallowedos,feature.EnableMergingPureDeltas,feature.disabledisallowedmsgs,"
    "feature.enableCitationsForSynthesisData,feature.EnableConversationShareApis,"
    "feature.EnableConversationShareApisForMsa,feature.EnableAmsArtifactsInShare,"
    "feature.EnableShareTypeLookupOnRedeem,feature.EnableShareTokenIdInContentApi,feature.EnableGoodbyeDrainGate,"
    "feature.enableGenerateGraphicArtOptionsSet,cdximagen,feature.EnableUpdatedUXForConfirmationDialog,"
    "cdxenablerenderforisocomp,feature.EnableDesignEditorImageGrounding,feature.EnableDesignerEditor,"
    "feature.EnableSkipRehydrationForSpeCIdImages,feature.sourcescontrolmainline,feature.sourcescontrolmainlineal,"
    "rich_responses,feature.EnableBase64DataInMessageAnnotations,feature.EnableSkipEmittingMessageOnFlush,"
    "feature.EnableRemoveEmptySourceAttributions,feature.EnableRemoveStreamingMode"
)

OPTIONS_SETS = [
    "update_textdoc_response_after_streaming", "enable_search_result_progress_messages", "cwc_flux_image",
    "cwc_code_interpreter", "cwc_code_interpreter_amsfix", "enable_msa_user", "cwcgptv", "gptvnorm2048",
    "pdnascan", "cwc_code_interpreter_citation_fix", "code_interpreter_interactive_charts",
    "code_interpreter_matplotlib_patching", "cwc_fileupload_odb", "update_memory_plugin",
    "add_custom_instructions", "cwc_flux_v3", "flux_v3_progress_messages", "enable_batch_token_processing",
    "enable_gg_gpt", "enable_inferred_memory_read", "cwc_table_context", "flux_v3_references",
    "flux_v3_references_entities", "flux_v3_references_ci", "add_filestore_filetype",
    "flux_v3_image_gen_enable_story", "rich_responses", "web_search_citations_answer_cards",
]

ALLOWED_MESSAGE_TYPES = [
    "Chat", "Suggestion", "InternalSearchQuery", "Disengaged", "InternalLoaderMessage", "Progress",
    "GeneratedCode", "RenderCardRequest", "AdsQuery", "SemanticSerp", "GenerateContentQuery",
    "GenerateGraphicArt", "SearchQuery", "ConfirmationCard", "AuthError", "DeveloperLogs", "TriggerPlugin",
    "HintInvocation", "MemoryUpdate", "EndOfRequest", "TriggerConfirmation", "ResumeInvokeAction",
    "ResumeUserInputRequest", "TriggerUserInputRequest", "EscapeHatch", "TriggerPluginAuth",
    "ResumePluginAuth", "SideBySide", "ReferencesListComplete", "SwitchRespondingEndpoint",
]


def _client_info(session_id: str) -> dict:
    # Both front-ends share the Office web shell; the two branding fields are
    # overridable in case the licensed client reports different ones.
    return {
        "clientPlatform": os.environ.get("COPILOT_CLIENT_PLATFORM", "copilotcom-web"),
        "clientAppName": "Office",
        "clientEntrypoint": os.environ.get("COPILOT_CLIENT_ENTRYPOINT", "copilotcom-officeweb"),
        "clientSessionId": session_id,
        "ProductCategory": "Chat",
        "clientAppType": "Web",
        "productEntryPoint": "ChatPanel",
        "deviceOS": "Windows",
        "deviceType": "Desktop",
        "clientPlatformVersion": "10",
    }


def _location_info() -> dict:
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    return {
        "timeZoneOffset": offset.total_seconds() / 3600 if offset else 0,
        "timeZone": now.tzname() or "UTC",
    }


def chat_frame(text: str, request_id: str, session_id: str, start_of_session: bool) -> dict:
    """The ``chat`` invocation the web client sends for one user message."""
    client_info = _client_info(session_id)
    return {
        "arguments": [{
            "source": "officeweb",
            "clientCorrelationId": request_id,
            "sessionId": session_id,
            "optionsSets": OPTIONS_SETS,
            "streamingMode": "ConciseWithPadding",
            "options": {},
            "extraExtensionParameters": {},
            "allowedMessageTypes": ALLOWED_MESSAGE_TYPES,
            "sliceIds": [],
            "threadLevelGptId": {},
            "requestId": request_id,
            "traceId": request_id,
            "isStartOfSession": start_of_session,
            "clientInfo": client_info,
            "message": {
                "author": "user",
                "inputMethod": "Keyboard",
                "text": text,
                "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
                "requestId": request_id,
                "locationInfo": _location_info(),
                "locale": "en-us",
                "messageType": "Chat",
                "experienceType": "Default",
                "adaptiveCards": [],
                "clientPreferences": {"executionControls": {"web": {}, "work": {}}},
                "connectedFederatedConnections": ["dummyId"],
                "clientInfo": client_info,
            },
            "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
            "isSbsSupported": True,
            "tone": "Magic",
            "renderReferencesBehindEOS": True,
            "disconnectBehavior": "continue",
        }],
        "invocationId": "0",
        "target": "chat",
        "type": 4,
    }


def encode(record: dict) -> bytes:
    """Serialise one SignalR record (JSON + record separator)."""
    return (json.dumps(record) + RECORD_SEPARATOR).encode()


def build_hub_url(
    oid: str,
    tenant: str,
    conversation_id: str,
    request_id: str,
    session_id: str,
    access_token: str,
    profile=None,
) -> str:
    """The chat socket URL for one turn (auth + routing + feature flags).

    ``profile`` defaults to :func:`copilot.profile.current_profile`; its
    ``hub_params`` select the consumer or the licensed experience. The URL is
    built here rather than in the driver so both transports (and tests) share it.
    """
    if profile is None:
        from .profile import current_profile

        profile = current_profile()
    query = {
        "chatsessionid": request_id,
        "XRoutingParameterSessionKey": oid,
        "clientrequestid": request_id,
        "X-SessionId": session_id,
        "ConversationId": conversation_id,
        "access_token": access_token,
        "variants": VARIANTS,
        **profile.hub_params,
    }
    return (CHATHUB_URL.format(oid=oid, tenant=tenant) + "?"
            + urlencode(query, quote_via=quote, safe=","))


HANDSHAKE = encode({"protocol": "json", "version": 1})
PING = encode({"type": 6})


def split_records(payload: Union[str, bytes]) -> List[dict]:
    """Decode a socket frame into its SignalR records (non-JSON is skipped)."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    records = []
    for part in payload.split(RECORD_SEPARATOR):
        part = part.strip()
        if not part:
            continue
        try:
            obj = json.loads(part)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


# Messages that are the bot's answer, as opposed to progress notes
# ("Hang on a sec…"), suggestions, search queries or other side channels.
def _is_reply(message: dict) -> bool:
    return (
        message.get("author") == "bot"
        and message.get("messageType") in (None, "Chat")
        and isinstance(message.get("text"), str)
        and message.get("contentOrigin") != "EarlyProgress"
        and message.get("contentType") != "EarlyProgress"
    )


class TurnParser:
    """Turn one chat turn's records into incremental reply text.

    The reply arrives as a first ``update`` carrying the opening text, then
    ``writeAtCursor`` deltas, then full-text snapshots (``isLastUpdate`` and the
    final ``type:2`` item). Snapshots only contribute what the deltas missed, so
    nothing is emitted twice. ``done`` / ``error`` / ``conversation_id`` fill in
    as the corresponding records arrive.
    """

    def __init__(self) -> None:
        self.text = ""
        self.conversation_id: Optional[str] = None
        self.done = False
        self.error: Optional[str] = None
        self.started_at = time.time()

    def feed(self, record: dict) -> List[str]:
        kind = record.get("type")
        if kind == 1 and record.get("target") == "update":
            chunks: List[str] = []
            for arg in record.get("arguments") or []:
                if isinstance(arg, dict):
                    chunks.extend(self._update(arg))
            return chunks
        if kind == 2:  # final result of the `chat` invocation
            item = record.get("item") or {}
            self._note_conversation(item)
            result = item.get("result") or {}
            if result.get("value") not in (None, "Success"):
                self.error = result.get("message") or result.get("value")
            replies = [m["text"] for m in item.get("messages") or [] if isinstance(m, dict) and _is_reply(m)]
            return self._sync(replies[-1]) if replies else []
        if kind == 3:  # invocation completion
            if record.get("error"):
                self.error = record["error"]
            self.done = True
        elif kind == 7:  # server closed the hub
            self.error = record.get("error") or "Copilot closed the chat socket"
            self.done = True
        return []

    def _update(self, arg: dict) -> List[str]:
        self._note_conversation(arg)
        chunks: List[str] = []
        delta = arg.get("writeAtCursor")
        if isinstance(delta, str) and delta:
            self.text += delta
            chunks.append(delta)
        for message in arg.get("messages") or []:
            if isinstance(message, dict) and _is_reply(message):
                chunks.extend(self._sync(message["text"]))
        return chunks

    def _sync(self, full: str) -> List[str]:
        """Emit the part of a full-text snapshot not yet streamed."""
        if not full.startswith(self.text):
            return []  # a rewrite of earlier text; keep what was already emitted
        new = full[len(self.text):]
        self.text = full
        return [new] if new else []

    def _note_conversation(self, obj: dict) -> None:
        if not self.conversation_id and obj.get("conversationId"):
            self.conversation_id = obj["conversationId"]
