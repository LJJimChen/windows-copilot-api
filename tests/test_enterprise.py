"""Tests for the consumer/enterprise profile split (no network, no browser)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot import CopilotClient
from copilot.auth import auth_from_token_response, load_auth, refresh_request
from copilot.browser import _is_login_host
from copilot.driver import (
    AuthExpired, ClearanceRequired, Copilot, LicenseRequired,
)
from copilot.profile import (
    CONSUMERS_TENANT, MSA_TENANT, current_profile, tenant_from_anchor,
    token_tenant_for,
)
from copilot.protocol import HUB_PARAMS, build_hub_url, chat_frame

ORG_TENANT = "11111111-2222-3333-4444-555555555555"


class ProfileTests(unittest.TestCase):
    def test_consumer_is_the_default_and_matches_the_old_constants(self):
        with patch.dict(os.environ, {}, clear=True):
            p = current_profile()
        self.assertEqual(p.name, "consumer")
        self.assertEqual(p.web_origin, "https://copilot.com")
        self.assertEqual(p.chat_url, "https://copilot.com/chat")
        self.assertEqual(p.default_tenant, MSA_TENANT)
        # Byte-for-byte the same routing params the consumer driver used before.
        self.assertEqual(p.hub_params, HUB_PARAMS)

    def test_enterprise_profile_switches_the_licensed_experience(self):
        with patch.dict(os.environ, {"COPILOT_PROFILE": "enterprise"}, clear=True):
            p = current_profile()
        self.assertEqual(p.name, "enterprise")
        self.assertEqual(p.web_origin, "https://m365.cloud.microsoft")
        self.assertEqual(p.chat_url, "https://m365.cloud.microsoft/chat")
        self.assertEqual(p.hub_params["licenseType"], "Copilot")
        self.assertEqual(p.hub_params["scenario"], "OfficeWebCopilot")
        # Work accounts must supply their own tenant; failing loudly beats
        # quietly talking to the consumer tenant.
        self.assertIsNone(p.default_tenant)

    def test_unknown_profile_name_falls_back_to_consumer(self):
        with patch.dict(os.environ, {"COPILOT_PROFILE": "nonsense"}, clear=True):
            self.assertEqual(current_profile().name, "consumer")

    def test_individual_overrides_win_over_the_preset(self):
        env = {
            "COPILOT_PROFILE": "enterprise",
            "COPILOT_TENANT": ORG_TENANT,
            "COPILOT_WEB_ORIGIN": "https://m365.cloud.microsoft/",
            "COPILOT_LICENSE_TYPE": "M365Copilot",
            "COPILOT_SCENARIO": "OfficeWebCopilotPreview",
            "COPILOT_CLIENT_PLATFORM": "m365web",
        }
        with patch.dict(os.environ, env, clear=True):
            p = current_profile()
        self.assertEqual(p.default_tenant, ORG_TENANT)
        self.assertEqual(p.web_origin, "https://m365.cloud.microsoft")  # trailing / trimmed
        self.assertEqual(p.hub_params["licenseType"], "M365Copilot")
        self.assertEqual(p.hub_params["scenario"], "OfficeWebCopilotPreview")
        # Unrelated params keep their preset values.
        self.assertEqual(p.hub_params["product"], "Office")

    def test_client_info_branding_is_overridable(self):
        with patch.dict(os.environ, {"COPILOT_CLIENT_PLATFORM": "m365web"}, clear=True):
            frame = chat_frame("hi", "req", "sess", True)
        info = frame["arguments"][0]["clientInfo"]
        self.assertEqual(info["clientPlatform"], "m365web")

    def test_hub_url_carries_tenant_and_licensed_params(self):
        with patch.dict(os.environ, {"COPILOT_PROFILE": "enterprise"}, clear=True):
            url = build_hub_url("oid-1", ORG_TENANT, "conv-1", "req-1", "sess-1", "tok-1")
        self.assertIn("/Chathub/oid-1@" + ORG_TENANT, url)
        self.assertIn("licenseType=Copilot", url)
        self.assertIn("scenario=OfficeWebCopilot", url)

    def test_consumer_hub_url_is_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            url = build_hub_url("oid-1", MSA_TENANT, "conv-1", "req-1", "sess-1", "tok-1")
        self.assertIn("/Chathub/oid-1@" + MSA_TENANT, url)
        self.assertIn("licenseType=Free", url)
        self.assertIn("scenario=OfficeWebFreeConsumerCopilot", url)


class TenantTests(unittest.TestCase):
    def test_tenant_is_recovered_from_the_sign_in_anchor(self):
        self.assertEqual(tenant_from_anchor("Oid:abc@" + ORG_TENANT), ORG_TENANT)
        self.assertEqual(tenant_from_anchor("Oid:" + MSA_TENANT + "@" + MSA_TENANT), MSA_TENANT)
        self.assertIsNone(tenant_from_anchor(None))
        self.assertIsNone(tenant_from_anchor("Oid:abc"))
        self.assertIsNone(tenant_from_anchor(""))

    def test_token_endpoint_follows_the_account_kind(self):
        self.assertEqual(token_tenant_for(MSA_TENANT), CONSUMERS_TENANT)
        self.assertEqual(token_tenant_for(None), CONSUMERS_TENANT)
        self.assertEqual(token_tenant_for(ORG_TENANT), ORG_TENANT)

    def test_auth_snapshot_keeps_the_tenant(self):
        body = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        auth = auth_from_token_response(body, "oid-1", "Oid:oid-1@" + ORG_TENANT, 1000.0)
        self.assertEqual(auth["tenant"], ORG_TENANT)
        explicit = auth_from_token_response(body, "oid-1", None, 1000.0, tenant=ORG_TENANT)
        self.assertEqual(explicit["tenant"], ORG_TENANT)

    def test_refresh_uses_the_org_tenant_and_the_right_origin(self):
        auth = {"refresh_token": "r", "oid": "o", "tenant": ORG_TENANT,
                "anchor": "Oid:o@" + ORG_TENANT}
        with patch.dict(os.environ, {"COPILOT_PROFILE": "enterprise"}, clear=True):
            url, data, headers = refresh_request(auth)
        self.assertIn(f"/{ORG_TENANT}/oauth2/v2.0/token", url)
        self.assertEqual(headers["Origin"], "https://m365.cloud.microsoft")
        self.assertEqual(data["X-AnchorMailbox"], "Oid:o@" + ORG_TENANT)

    def test_consumer_refresh_keeps_the_consumers_endpoint(self):
        auth = {"refresh_token": "r", "oid": "o", "tenant": MSA_TENANT}
        with patch.dict(os.environ, {}, clear=True):
            url, data, headers = refresh_request(auth)
        self.assertIn(f"/{CONSUMERS_TENANT}/oauth2/v2.0/token", url)
        self.assertEqual(headers["Origin"], "https://copilot.com")

    def test_refresh_is_skipped_when_the_tenant_is_unknown(self):
        # A work account with no captured tenant must not guess an endpoint.
        auth = {"refresh_token": "r", "oid": "o"}
        with patch.dict(os.environ, {"COPILOT_PROFILE": "enterprise"}, clear=True):
            self.assertIsNone(refresh_request(auth))

    def test_cached_session_without_a_tenant_migrates_from_the_anchor(self):
        auth = {"access_token": "a", "refresh_token": "r", "oid": "o",
                "anchor": "Oid:o@" + ORG_TENANT, "expires_at": time.time() + 3600,
                "saved_at": time.time()}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            path.write_text(json.dumps(auth), encoding="utf-8")
            loaded = load_auth(path=str(path), auto_login=False)
        self.assertEqual(loaded["tenant"], ORG_TENANT)


class ClientPropagationTests(unittest.TestCase):
    def test_client_forwards_the_account_tenant_to_the_driver(self):
        seen = {}

        def fake_create(prompt, **kwargs):
            seen.update(kwargs)
            yield "ok"

        client = CopilotClient(interactive_clear=False)
        client._auth = {
            "access_token": "tok", "oid": "oid-1", "tenant": ORG_TENANT,
            "saved_at": time.time(), "expires_at": time.time() + 3600,
        }
        with patch.object(client._driver, "create_completion", side_effect=fake_create):
            reply = client.chat("hi")
        self.assertEqual(seen["tenant"], ORG_TENANT)
        self.assertEqual(reply.text, "ok")


class ErrorClassificationTests(unittest.TestCase):
    def test_licence_errors_are_recognised(self):
        for message in ("no license assigned", "user is not entitled",
                        "subscription required for Copilot"):
            with self.assertRaises(LicenseRequired):
                Copilot._raise_error(message)

    def test_auth_errors_are_recognised(self):
        for message in ("unauthorized", "AADSTS70008: expired", "invalid_grant"):
            with self.assertRaises(AuthExpired):
                Copilot._raise_error(message)

    def test_verification_errors_are_still_recognised(self):
        with self.assertRaises(ClearanceRequired):
            Copilot._raise_error("turnstile challenge required")

    def test_unknown_errors_stay_generic(self):
        with self.assertRaises(RuntimeError) as ctx:
            Copilot._raise_error("something odd happened")
        self.assertNotIsInstance(ctx.exception, (LicenseRequired, AuthExpired,
                                                 ClearanceRequired))


class LoginHostTests(unittest.TestCase):
    def test_microsoft_and_federated_sign_in_hosts(self):
        self.assertTrue(_is_login_host("login.microsoftonline.com"))
        self.assertTrue(_is_login_host("login.microsoftonline.us"))
        self.assertTrue(_is_login_host("login.live.com"))
        self.assertFalse(_is_login_host("m365.cloud.microsoft"))

    def test_extra_hosts_come_from_the_environment(self):
        with patch.dict(os.environ, {"COPILOT_LOGIN_HOSTS": "adfs.contoso.com"}, clear=True):
            self.assertTrue(_is_login_host("adfs.contoso.com"))
            self.assertTrue(_is_login_host("sso.adfs.contoso.com"))
            self.assertFalse(_is_login_host("evil-adfs.contoso.com.example"))


class ServerLicenseMappingTests(unittest.TestCase):
    def test_licence_failure_is_a_403_not_a_502(self):
        from server import api
        from server.schemas import ChatCompletionRequest, ChatMessage

        req = ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])
        with patch.object(api.client, "chat", side_effect=LicenseRequired("no seat")):
            with patch.object(api._rate_limiter, "try_acquire", return_value=(True, 0.0)):
                response = api.chat_completions(req)
        self.assertEqual(response.status_code, 403)
        self.assertIn("license_required", response.body.decode())


if __name__ == "__main__":
    unittest.main()
