"""Tests for scripts/probe.py against the fake IMAP server and token endpoint."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import sys
import unittest
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from fake_imap import FakeIMAPServer, FakeTokenEndpoint, Mailbox, Message  # noqa: E402

spec = importlib.util.spec_from_file_location("probe", Path(__file__).parents[1] / "scripts" / "probe.py")
probe = importlib.util.module_from_spec(spec)
sys.modules["probe"] = probe
spec.loader.exec_module(probe)

USER = "student@example.edu"


def mailbox(**overrides) -> Mailbox:
    today = date.today()
    messages = [
        Message(1, today - timedelta(days=40), "Old notice", flags={"\\Seen"}),
        Message(2, today - timedelta(days=3), "Homework 4"),
        Message(3, today - timedelta(days=1), "Exam moved", flags={"\\Seen"}),
        Message(4, today, "Lab report"),
    ]
    return Mailbox(user=USER, messages=messages, **overrides)


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.server = None
        self.tokens = None

    def tearDown(self):
        for server in (self.server, self.tokens):
            if server:
                server.close()

    def run_probe(self, box: Mailbox, *args: str, error: dict | None = None, paste=None):
        self.server = FakeIMAPServer(box)
        self.tokens = FakeTokenEndpoint(code="good-code", access_token="refreshed-access", error=error)
        provider = dataclasses.replace(
            probe.PROVIDERS["microsoft"],
            host="127.0.0.1",
            port=self.server.port,
            token_endpoint=self.tokens.url,
            tls=False,
        )
        captured: dict[str, str] = {}

        def answer(prompt: str = "") -> str:
            print(prompt)
            if "Redirect URL" in prompt:
                url = stdout.getvalue().split("sign in:\n\n", 1)[1].split("\n", 1)[0]
                captured["url"] = url
                state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
                return paste(state) if paste else f"https://localhost/?code=good-code&state={state}&session_state=x"
            raise AssertionError(f"unexpected prompt: {prompt}")

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(probe.PROVIDERS, {"microsoft": provider}), \
                mock.patch("builtins.input", answer), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = probe.main(["microsoft", "--user", USER, "--idle", "1", *args])
        return code, stdout.getvalue() + stderr.getvalue(), captured.get("url", "")

    def test_go_with_oauth(self):
        box = mailbox(token="refreshed-access", idle_event_delay=0.2)
        code, output, url = self.run_probe(box, "--yes")
        self.assertEqual(code, 0, output)
        self.assertIn("Verdict: GO", output)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["client_id"], ["9e5f94bc-e8a4-4e73-b8be-63364c29d753"])
        self.assertEqual(query["redirect_uri"], ["https://localhost"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("offline_access", query["scope"][0])
        # The IMAP login uses the token from the refresh grant.
        self.assertIn("code_verifier", self.tokens.requests[0])
        self.assertEqual(self.tokens.requests[1]["grant_type"], "refresh_token")
        self.assertIn("1 new-mail events", output)

    def test_reads_only_the_window_and_peeks(self):
        box = mailbox(token="refreshed-access")
        code, output, _ = self.run_probe(box, "--no-seen-test")
        self.assertEqual(code, 2, output)
        self.assertIn("3 of 4 messages", output)
        fetches = [command for command in box.commands if " UID FETCH " in command]
        body_fetches = [command for command in fetches if "BODY" in command]
        self.assertTrue(body_fetches)
        self.assertTrue(all("BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)]" in command for command in body_fetches))
        self.assertFalse(any("RFC822)" in command or "BODY[]" in command or "BODY.PEEK[]" in command for command in fetches))
        self.assertFalse(any(" UID STORE " in command for command in box.commands))
        self.assertEqual({m.uid for m in box.messages if "\\Seen" in m.flags}, {1, 3})
        self.assertNotIn("uid 1 ", output)

    def test_seen_test_restores_the_flag(self):
        box = mailbox(token="refreshed-access")
        code, output, _ = self.run_probe(box, "--yes")
        self.assertEqual(code, 0, output)
        self.assertNotIn("\\Seen", box.messages[3].flags)
        stores = [command for command in box.commands if " UID STORE " in command]
        self.assertEqual([command.split()[4] for command in stores], ["+FLAGS.SILENT", "-FLAGS.SILENT"])

    def test_conditional_access_error_is_no_go(self):
        error = {"error": "invalid_grant", "error_description": "AADSTS53003: Access has been blocked by Conditional Access policies.\r\nTrace ID: x"}
        code, output, _ = self.run_probe(mailbox(), error=error)
        self.assertEqual(code, 1, output)
        self.assertIn("A Conditional Access policy blocks this sign-in", output)
        self.assertIn("Verdict: NO GO", output)

    def test_state_mismatch_is_rejected(self):
        code, output, _ = self.run_probe(mailbox(), paste=lambda state: "https://localhost/?code=good-code&state=other")
        self.assertEqual(code, 1, output)
        self.assertIn("state value", output)

    def test_bad_imap_token_is_no_go(self):
        box = mailbox(token="a-different-token")
        code, output, _ = self.run_probe(box, "--yes")
        self.assertEqual(code, 1, output)
        self.assertIn("[FAIL] imap-login", output)
        self.assertIn("401", output)

    def test_tokens_are_never_printed(self):
        box = mailbox(token="refreshed-access")
        _, output, _ = self.run_probe(box, "--yes")
        for value in ("first-access", "refresh-1", "refresh-2", "refreshed-access"):
            self.assertNotIn(value, output)

    def test_password_sign_in(self):
        box = mailbox(password="app-password")
        self.server = FakeIMAPServer(box)
        provider = dataclasses.replace(probe.PROVIDERS["google"], host="127.0.0.1", port=self.server.port, tls=False)
        stdout = io.StringIO()
        with mock.patch.dict(probe.PROVIDERS, {"google": provider}), \
                mock.patch("getpass.getpass", return_value="app-password"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            code = probe.main(["google", "--user", USER, "--auth", "password", "--idle", "1", "--yes"])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertNotIn("app-password", stdout.getvalue())

    def test_compact_set(self):
        self.assertEqual(probe.compact_set([9, 3, 4, 5, 7, 8]), "3:5,7:9")
        self.assertEqual(probe.compact_set([1]), "1")


if __name__ == "__main__":
    unittest.main()
