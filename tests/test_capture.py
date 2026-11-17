"""Tests for the enterprise capture helper (pure functions, no browser)."""

import json
import unittest

from tests.capture_enterprise import (
    records, suggest_overrides, summarise_chat_frame, summarise_hub_url,
    summarise_token_request,
)
from copilot.profile import current_profile


class RecordTests(unittest.TestCase):
    def test_splits_signalr_records_and_skips_junk(self):
        payload = ('{"type":1}\x1e{"type":3}\x1enot-json\x1e \x1e').encode()
        self.assertEqual(records(payload), [{"type": 1}, {"type": 3}])

    def test_accepts_str_payloads(self):
        self.assertEqual(records('{"type":6}\x1e'), [{"type": 6}])


class HubUrlTests(unittest.TestCase):
    URL = ("wss://substrate.office.com/m365Copilot/Chathub/oid-1@tenant-1"
           "?chatsessionid=req&access_token=SECRET-TOKEN"
           "&licenseType=Copilot&scenario=OfficeWebCopilot&agentHost=Bizchat.FullScreen"
           "&source=%22officeweb%22&product=Office&agent=web&isEdu=false"
           "&variants=featureA,featureB")

    def test_routing_params_are_kept_and_secrets_are_not(self):
        summary = summarise_hub_url(self.URL)
        self.assertEqual(summary["path_tenant"], "tenant-1")
        self.assertEqual(summary["query"]["licenseType"], "Copilot")
        self.assertEqual(summary["query"]["scenario"], "OfficeWebCopilot")
        self.assertEqual(summary["variants"]["length"], len("featureA,featureB"))
        self.assertEqual(summary["variants"]["value"], "featureA,featureB")
        self.assertIn("chatsessionid", summary["other_query_keys"])
        self.assertIn("access_token", summary["other_query_keys"])
        dumped = json.dumps(summary)
        # Only the *names* of the auth parameter and the oid survive, never values.
        self.assertNotIn("SECRET-TOKEN", dumped)
        self.assertNotIn("oid-1", dumped)


class ChatFrameTests(unittest.TestCase):
    def frame(self, text="my private prompt"):
        return {
            "type": 4, "target": "chat", "invocationId": "0",
            "arguments": [{
                "source": "officeweb",
                "optionsSets": ["a", "b"],
                "allowedMessageTypes": ["Chat", "EndOfRequest"],
                "streamingMode": "ConciseWithPadding",
                "tone": "Magic",
                "plugins": [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
                "isStartOfSession": True,
                "isSbsSupported": True,
                "disconnectBehavior": "continue",
                "threadLevelGptId": {},
                "clientInfo": {
                    "clientPlatform": "m365web",
                    "clientEntrypoint": "m365web-officeweb",
                    "clientSessionId": "session-uuid",
                    "clientAppName": "Office",
                },
                "message": {"author": "user", "text": text, "messageType": "Chat"},
            }],
        }

    def test_client_description_is_captured_without_the_prompt(self):
        summary = summarise_chat_frame(self.frame())
        self.assertEqual(summary["clientInfo"]["clientPlatform"], "m365web")
        self.assertNotIn("clientSessionId", summary["clientInfo"])
        self.assertIn("text", summary["message_keys"])
        self.assertEqual(summary["tone"], "Magic")
        self.assertNotIn("my private prompt", json.dumps(summary))

    def test_other_frames_are_ignored(self):
        self.assertIsNone(summarise_chat_frame({"type": 1, "target": "update"}))


class TokenRequestTests(unittest.TestCase):
    def test_only_public_ids_and_no_tokens(self):
        post = ("client_id=abc&brk_client_id=def&redirect_uri=brk-multihub%3A%2F%2Fx"
                "&scope=substrate&grant_type=refresh_token&refresh_token=SECRET-REFRESH"
                "&X-AnchorMailbox=Oid%3Aoid%40tenant")
        headers = {"Origin": "https://m365.cloud.microsoft", "Cookie": "SECRET-COOKIE",
                   "Content-Type": "application/x-www-form-urlencoded"}
        summary = summarise_token_request(
            "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token",
            post, headers, 200, ["access_token", "refresh_token", "expires_in"],
        )
        self.assertEqual(summary["url_tenant"], "tenant-1")
        self.assertEqual(summary["form"]["client_id"], "abc")
        self.assertEqual(summary["header_values"]["origin"], "https://m365.cloud.microsoft")
        self.assertIn("access_token", summary["response_keys"])
        dumped = json.dumps(summary)
        for secret in ("SECRET-REFRESH", "SECRET-COOKIE", "oid%40tenant"):
            self.assertNotIn(secret, dumped)


class OverrideTests(unittest.TestCase):
    def test_differences_become_env_overrides(self):
        with_id = {"COPILOT_PROFILE": "enterprise"}
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, with_id, clear=True):
            profile = current_profile()
        overrides = suggest_overrides(
            {"licenseType": "M365Copilot", "scenario": "OfficeWebCopilot",
             "product": "Office"},
            {"clientPlatform": "m365web", "clientEntrypoint": "copilotcom-officeweb"},
            profile,
        )
        self.assertEqual(overrides["COPILOT_LICENSE_TYPE"], "M365Copilot")
        self.assertEqual(overrides["COPILOT_CLIENT_PLATFORM"], "m365web")
        # Unchanged values and the default entrypoint produce no override.
        self.assertNotIn("COPILOT_SCENARIO", overrides)
        self.assertNotIn("COPILOT_PRODUCT", overrides)
        self.assertNotIn("COPILOT_CLIENT_ENTRYPOINT", overrides)

    def test_matching_capture_produces_no_overrides(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"COPILOT_PROFILE": "enterprise"}, clear=True):
            profile = current_profile()
        self.assertEqual(
            suggest_overrides(profile.hub_params,
                              {"clientPlatform": "copilotcom-web",
                               "clientEntrypoint": "copilotcom-officeweb"},
                              profile),
            {},
        )


if __name__ == "__main__":
    unittest.main()
