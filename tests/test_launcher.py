"""Tests for the browser launcher (which installed browser gets driven)."""

import os
import unittest
from unittest.mock import MagicMock, patch

from copilot import launcher


def env(**values):
    return patch.dict(os.environ, values, clear=True)


class ChannelTests(unittest.TestCase):
    def test_edge_is_the_default(self):
        with env():
            self.assertEqual(launcher.browser_channel(), "msedge")
            self.assertIsNone(launcher.browser_executable())

    def test_channel_can_be_changed_or_opted_out_of(self):
        with env(COPILOT_BROWSER_CHANNEL="chrome"):
            self.assertEqual(launcher.browser_channel(), "chrome")
        for value in ("bundled", "playwright", "none", "False"):
            with env(COPILOT_BROWSER_CHANNEL=value):
                self.assertIsNone(launcher.browser_channel())
        # Blank is treated as unset -> the default (Edge).
        with env(COPILOT_BROWSER_CHANNEL="  "):
            self.assertEqual(launcher.browser_channel(), "msedge")

    def test_executable_wins_over_the_channel(self):
        with env(COPILOT_BROWSER_CHANNEL="chrome", COPILOT_BROWSER_EXECUTABLE="/opt/edge"):
            self.assertEqual(launcher.browser_executable(), "/opt/edge")

    def test_describe_reports_the_fallback_only_for_the_default(self):
        with env():
            self.assertIn("msedge", launcher.describe())
            self.assertIn("bundled", launcher.describe())
        with env(COPILOT_BROWSER_CHANNEL="chrome"):
            self.assertEqual(launcher.describe(), "chrome (installed browser)")
        with env(COPILOT_BROWSER_EXECUTABLE="/opt/edge"):
            self.assertEqual(launcher.describe(), "/opt/edge")


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.playwright = MagicMock()
        self.context = object()
        self.playwright.chromium.launch_persistent_context.return_value = self.context

    def test_default_launch_uses_the_edge_channel(self):
        with env():
            context, label = launcher.launch_persistent_context(
                self.playwright, "/tmp/profile", headless=False)
        self.assertIs(context, self.context)
        self.assertIn("msedge", label)
        kwargs = self.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs["channel"], "msedge")
        self.assertFalse(kwargs["headless"])
        self.assertEqual(kwargs["ignore_default_args"], ["--enable-automation"])
        self.assertEqual(kwargs["args"], ["--disable-blink-features=AutomationControlled"])

    def test_missing_edge_falls_back_to_bundled_chromium(self):
        self.playwright.chromium.launch_persistent_context.side_effect = [
            RuntimeError("Executable doesn't exist"), self.context,
        ]
        with env():
            context, label = launcher.launch_persistent_context(
                self.playwright, "/tmp/profile")
        self.assertIs(context, self.context)
        self.assertIn("bundled", label)
        second = self.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertNotIn("channel", second)
        self.assertNotIn("executable_path", second)

    def test_an_explicit_channel_never_falls_back(self):
        self.playwright.chromium.launch_persistent_context.side_effect = RuntimeError("nope")
        with env(COPILOT_BROWSER_CHANNEL="chrome"):
            with self.assertRaises(RuntimeError) as ctx:
                launcher.launch_persistent_context(self.playwright, "/tmp/profile")
        self.assertEqual(
            self.playwright.chromium.launch_persistent_context.call_count, 1)
        self.assertIn("chrome", str(ctx.exception))

    def test_proxy_is_forwarded(self):
        proxy = {"server": "http://127.0.0.1:8080"}
        with env():
            launcher.launch_persistent_context(
                self.playwright, "/tmp/profile", proxy=proxy)
        kwargs = self.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs["proxy"], proxy)

    def test_explicit_arguments_override_the_environment(self):
        with env(COPILOT_BROWSER_CHANNEL="chrome"):
            _, label = launcher.launch_persistent_context(
                self.playwright, "/tmp/profile", channel=None)
        self.assertIn("bundled", label)
        self.assertNotIn("channel",
                         self.playwright.chromium.launch_persistent_context.call_args.kwargs)

    def test_failure_message_explains_both_options(self):
        self.playwright.chromium.launch_persistent_context.side_effect = RuntimeError("boom")
        with env():
            with self.assertRaises(RuntimeError) as ctx:
                launcher.launch_persistent_context(self.playwright, "/tmp/profile")
        message = str(ctx.exception)
        self.assertIn("Edge", message)
        self.assertIn("playwright install chromium", message)


if __name__ == "__main__":
    unittest.main()
