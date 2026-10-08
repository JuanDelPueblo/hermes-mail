"""Tests for hermes-maild against the fake IMAP server and token endpoint."""

from __future__ import annotations

import dataclasses
import imaplib
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))

from fake_imap import FakeIMAPServer, FakeTokenEndpoint, Mailbox, Message  # noqa: E402
from hermes_mail import auth, config  # noqa: E402
from hermes_mail.client import Client, MailServiceError  # noqa: E402
from hermes_mail.server import Service, SocketServer  # noqa: E402

USER = "student@example.edu"
PDF = b"%PDF-1.4 fake document " * 200


def wait_for(condition, timeout: float = 10.0, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


def sample_messages() -> list[Message]:
    today = date.today()
    return [
        Message(1, today - timedelta(days=40), "Old notice", flags={"\\Seen"}),
        Message(2, today - timedelta(days=3), "Homework 4", body="Submit homework 4 by Friday.\r\n"),
        Message(3, today - timedelta(days=1), "Exam moved", flags={"\\Seen"}, body="short",
                html="<html><head><style>p{}</style></head><body><p>The exam moved to <b>Monday</b>.</p>"
                     + "<p>" + "Details. " * 80 + "</p></body></html>"),
        Message(4, today, "Lab report", body="Attached is the lab rubric.\r\n",
                attachments=[("rubric é.pdf", "application/pdf", PDF)]),
    ]


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.box = Mailbox(user=USER, token="good-access", messages=sample_messages())
        self.imap = FakeIMAPServer(self.box)
        self.tokens = FakeTokenEndpoint(code="good-code", access_token="good-access")
        provider = dataclasses.replace(auth.PROVIDERS["microsoft"], token_endpoint=self.tokens.url)
        self.providers = mock.patch.dict(auth.PROVIDERS, {"microsoft": provider})
        self.providers.start()
        self.service = None
        self.server = None

    def tearDown(self):
        if self.service:
            self.service.stop()
            if not self.service.threads:
                # A started worker can still use the store while it stops.
                self.service.store.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.providers.stop()
        self.imap.close()
        self.tokens.close()
        self.tmp.cleanup()

    def make_service(self, *, signed_in: bool = True, extract: bool = True, web_settings: bool = True, retention: int | None = None,
                     **account) -> Service:
        self.raw = raw = {
            "state_dir": str(self.root / "state"),
            "socket": str(self.root / "mail.sock"),
            "export_dir": str(self.root / "exports"),
            "extract_root": str(self.root / "home") if extract else None,
            "web_settings": web_settings,
            **({} if retention is None else {"triage_retention_days": retention}),
            "accounts": {"uni": {"provider": "microsoft", "address": USER, "poll_seconds": 30, **account}},
        }
        cfg = config.parse(raw)
        if signed_in:
            auth.TokenStore(cfg.state_dir / "tokens").save("uni", {"refresh_token": "refresh-1"})
        port = self.imap.port
        self.service = Service(cfg, imap_factory=lambda host, _: imaplib.IMAP4("127.0.0.1", port, timeout=10))
        return self.service

    def start(self, **kwargs) -> Service:
        service = self.make_service(**kwargs)
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the first sync")
        return service

    def call(self, service: Service, **request):
        response = service.handle(request)
        self.assertTrue(response["ok"], response)
        return response

    def test_first_sync_reads_only_the_window_and_sends_no_events(self):
        service = self.start()
        listed = self.call(service, op="list")
        self.assertEqual([m["subject"] for m in listed["messages"]], ["Lab report", "Exam moved", "Homework 4"])
        self.assertEqual(self.call(service, op="events")["events"], [])
        record = service.store.get(listed["messages"][2]["id"])
        self.assertIn("Submit homework 4", record["preview"])
        exam = service.store.get(listed["messages"][1]["id"])
        self.assertIn("The exam moved to Monday", exam["preview"])
        self.assertNotIn("p{}", exam["preview"])

    def test_download_rules(self):
        self.start()
        commands = [command.split(" ", 1)[1] for command in self.box.commands if " " in command]
        fetches = [command for command in commands if command.upper().startswith("UID FETCH")]
        self.assertTrue(fetches)
        for command in fetches:
            self.assertNotRegex(command, r"BODY\[", command)
            self.assertNotIn("RFC822)", command)
            self.assertNotRegex(command, r"BODY\.PEEK\[\]", command)
            self.assertNotRegex(command, r"\bRFC822\b(?!\.SIZE)", command)
        # Previews read only the start of the text part.
        previews = [command for command in fetches if re.search(r"BODY\.PEEK\[\d", command)]
        self.assertTrue(previews)
        self.assertTrue(all(re.search(r"<0\.\d+>", command) for command in previews), previews)
        # The old message outside the window is never read.
        self.assertFalse(any(re.match(r"UID FETCH 1 ", command) for command in fetches))
        self.assertFalse(any(command.upper().startswith(("SELECT", "CLOSE", "EXPUNGE", "UID STORE")) for command in commands))
        self.assertEqual({m.uid for m in self.box.messages if "\\Seen" in m.flags}, {1, 3})

    def test_new_mail_makes_one_event(self):
        service = self.start()
        wait_for(lambda: self.box.idling.is_set(), message="IDLE")
        self.box.deliver(Message(5, date.today(), "Quiz tomorrow", body="Quiz on chapter 3.\r\n"))
        events = wait_for(lambda: service.store.pending_events(), message="a new-mail event")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "mail.new")
        record = service.store.get(events[0]["mail_id"])
        self.assertEqual(record["subject"], "Quiz tomorrow")
        self.assertEqual(self.call(service, op="ack", seqs=[events[0]["seq"]])["acked"], 1)
        self.assertEqual(service.store.pending_events(), [])

    def test_show_attachments_and_export(self):
        service = self.start()
        lab = self.call(service, op="list", query="Lab report")["messages"][0]
        shown = self.call(service, op="show", id=lab["id"])
        self.assertIn("lab rubric", shown["body"])
        self.assertEqual(shown["attachments"][0]["name"], "rubric é.pdf")
        self.assertEqual(shown["attachments"][0]["content_type"], "application/pdf")
        exported = self.call(service, op="export_attachment", id=lab["id"], index=0)
        self.assertEqual(Path(exported["path"]).read_bytes(), PDF)
        self.assertTrue(exported["path"].startswith(str(self.root / "exports")))
        again = self.call(service, op="export_attachment", id=lab["id"], index=0)
        self.assertTrue(again["cached"])
        extracted = self.call(service, op="extract_attachment", id=lab["id"], index=0, output="docs/rubric.pdf")
        self.assertEqual(Path(extracted["path"]).read_bytes(), PDF)
        refused = service.handle({"op": "extract_attachment", "id": lab["id"], "index": 0, "output": "/etc/passwd"})
        self.assertFalse(refused["ok"])
        missing = service.handle({"op": "export_attachment", "id": lab["id"], "index": 3})
        self.assertIn("out of range", missing["error"])
        # Reads never change the read state.
        self.assertNotIn("\\Seen", self.box.messages[3].flags)

    def test_html_body_is_used_when_plain_is_almost_empty(self):
        service = self.start()
        exam = self.call(service, op="list", query="Exam moved")["messages"][0]
        body = self.call(service, op="show", id=exam["id"])["body"]
        self.assertIn("The exam moved to Monday", body)

    def test_mark_read_and_unread(self):
        service = self.start()
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        before = len(self.box.commands)
        result = self.call(service, op="mark", ids=[homework["id"]], read=True)
        self.assertEqual(result["results"][homework["id"]], {"ok": True, "read": True})
        self.assertIn("\\Seen", self.box.messages[1].flags)
        self.assertTrue(service.store.get(homework["id"])["read"])
        self.call(service, op="mark", ids=[homework["id"]], read=False)
        self.assertNotIn("\\Seen", self.box.messages[1].flags)
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("SELECT") for command in commands))
        self.assertTrue(any(command.startswith("UNSELECT") for command in commands))
        self.assertFalse(any(command.startswith(("CLOSE", "EXPUNGE")) for command in commands))

    def test_mark_reports_unknown_ids(self):
        service = self.start()
        response = service.handle({"op": "mark", "ids": ["uni.0000000000000000"], "read": True})
        self.assertFalse(response["ok"])
        self.assertIn("message not found", response["error"])

    def test_folders_lists_selectable_folders_with_their_special_use(self):
        self.box.listing = [
            '(\\HasNoChildren) "/" "INBOX"',
            '(\\HasNoChildren \\Archive) "/" "Archive"',
            '(\\Noselect \\HasChildren) "/" "[Gmail]"',
            '(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            '(\\HasNoChildren) "/" "Clases/Programaci&APM-n"',
        ]
        service = self.start(archive_folder="Archive")
        before = len(self.box.commands)
        result = self.call(service, op="folders", account="uni")
        self.assertEqual(result["folders"], [
            {"name": "INBOX", "special": "inbox"},
            {"name": "Archive", "special": "archive"},
            {"name": "[Gmail]/All Mail", "special": "all"},
            {"name": "Clases/Programación", "special": ""},
        ])
        self.assertEqual((result["archive_folder"], result["synced"]), ("Archive", ["INBOX"]))
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("LIST") for command in commands))
        self.assertFalse(any(command.startswith(("SELECT", "UID", "STORE", "CLOSE", "EXPUNGE")) for command in commands))

    def test_triage_log_records_updates_and_lists(self):
        service = self.make_service()
        entry = {"account": "uni", "mail_id": "uni.abc", "message_id": "<m@x>", "subject": "Exam moved", "sender": "Prof <p@x.edu>",
                 "mode": "triage", "status": "notified", "decision": "notify", "reason": "A person wrote to the owner.",
                 "summary": "The exam moved to Monday.", "actions": [{"step": "export attachment 0", "ok": True}], "attachments": [0]}
        first = self.call(service, op="triage_record", entry=entry, event_seq=7)["id"]
        # The same event again updates the row. It adds no second row.
        again = self.call(service, op="triage_record", entry={**entry, "status": "error", "error": "send failed"}, event_seq=7)["id"]
        self.assertEqual(first, again)
        other = self.call(service, op="triage_record", entry={**entry, "mail_id": "uni.def", "subject": "Newsletter", "status": "silent",
                                                               "decision": "silent", "summary": "Weekly 100% news"}, event_seq=8)["id"]
        listed = self.call(service, op="triage_list")
        self.assertEqual([item["id"] for item in listed["entries"]], [other, first])
        shown = self.call(service, op="triage_show", id=first)
        self.assertEqual((shown["status"], shown["error"], shown["actions"], shown["attachments"]),
                         ("error", "send failed", [{"step": "export attachment 0", "ok": True}], [0]))
        self.assertTrue(shown["finished"] and shown["created"])
        self.assertNotIn("event_seq", shown)
        self.assertEqual([item["id"] for item in self.call(service, op="triage_list", status="silent")["entries"]], [other])
        self.assertEqual([item["id"] for item in self.call(service, op="triage_list", query="100%")["entries"]], [other])
        self.assertEqual([item["id"] for item in self.call(service, op="triage_list", query="exam")["entries"]], [first])
        self.assertEqual(self.call(service, op="triage_list", since="1h")["count"], 2)
        self.assertEqual(self.call(service, op="triage_list", account="nope")["count"], 0)
        self.call(service, op="triage_update", id=first, fields={"status": "notified", "error": ""})
        self.assertEqual(self.call(service, op="triage_show", id=first)["status"], "notified")

    def test_triage_log_keeps_a_dispatched_row_open_until_it_finishes(self):
        service = self.make_service()
        entry = {"account": "uni", "mail_id": "uni.abc", "mode": "agent", "status": "dispatched"}
        identifier = self.call(service, op="triage_record", entry=entry)["id"]
        self.assertEqual(self.call(service, op="triage_show", id=identifier)["finished"], "")
        self.call(service, op="triage_update", id=identifier, fields={"status": "no_report", "error": "no answer"})
        self.assertTrue(self.call(service, op="triage_show", id=identifier)["finished"])

    def test_an_agent_run_reports_once(self):
        service = self.make_service()
        entry = {"account": "uni", "mail_id": "uni.abc", "subject": "Exam moved", "mode": "agent", "status": "dispatched", "run_id": "1-abc"}
        identifier = self.call(service, op="triage_record", entry=entry, event_seq=1)["id"]
        report = {"status": "notified", "decision": "notify", "reason": "A person wrote.", "summary": "The exam moved.",
                  "actions": [{"step": "create task", "ok": True}]}
        row = self.call(service, op="triage_report", run_id="1-abc", fields=report)
        self.assertEqual((row["id"], row["status"], row["summary"], row["subject"], row["run_id"]), (identifier, "notified", "The exam moved.", "Exam moved", "1-abc"))
        self.assertTrue(row["finished"])
        self.assertEqual(row["actions"], [{"step": "create task", "ok": True}])
        again = service.handle({"op": "triage_report", "run_id": "1-abc", "fields": report})
        self.assertFalse(again["ok"])
        self.assertIn("already reported", again["error"])
        self.assertEqual(self.call(service, op="triage_show", id=identifier)["summary"], "The exam moved.")

    def test_a_report_needs_the_run_id_of_an_open_entry(self):
        service = self.make_service()
        self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.abc", "status": "silent"}, event_seq=1)
        self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.def", "status": "dispatched", "run_id": "2-def"}, event_seq=2)
        for request, text in (
            ({"run_id": "nope", "fields": {"status": "silent"}}, "unknown run ID"),
            ({"run_id": "", "fields": {"status": "silent"}}, "run_id and fields"),
            ({"run_id": "2-def", "fields": {"status": "error"}}, "notified or silent"),
            ({"run_id": "2-def", "fields": "text"}, "run_id and fields"),
        ):
            with self.subTest(request=request):
                response = service.handle({"op": "triage_report", **request})
                self.assertFalse(response["ok"])
                self.assertIn(text, response["error"])
        # An entry without a run ID can never be claimed with an empty ID.
        self.assertFalse(service.handle({"op": "triage_report", "run_id": "", "fields": {"status": "silent"}})["ok"])

    def test_a_closed_run_cannot_report(self):
        service = self.make_service()
        identifier = self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.abc", "status": "dispatched", "run_id": "1-abc"})["id"]
        self.call(service, op="triage_update", id=identifier, fields={"status": "no_report"}, expect_status="dispatched")
        refused = service.handle({"op": "triage_report", "run_id": "1-abc", "fields": {"status": "notified"}})
        self.assertFalse(refused["ok"])
        self.assertIn("timed out", refused["error"])

    def test_update_can_require_the_status(self):
        service = self.make_service()
        identifier = self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.abc", "status": "notified"})["id"]
        response = service.handle({"op": "triage_update", "id": identifier, "fields": {"status": "no_report"}, "expect_status": "dispatched"})
        self.assertFalse(response["ok"])
        self.assertIn("with the status dispatched", response["error"])
        self.assertEqual(self.call(service, op="triage_show", id=identifier)["status"], "notified")

    def test_triage_log_refuses_bad_requests(self):
        service = self.make_service()
        good = {"account": "uni", "mail_id": "uni.abc", "status": "silent"}
        for request in (
            {"op": "triage_record", "entry": "text"},
            {"op": "triage_record", "entry": {**good, "status": "great"}},
            {"op": "triage_record", "entry": {"status": "silent", "account": "uni"}},
            {"op": "triage_record", "entry": good, "event_seq": "one"},
            {"op": "triage_update", "id": 99, "fields": {"status": "silent"}},
            {"op": "triage_update", "id": 1, "fields": {"status": "great"}},
            {"op": "triage_show", "id": 99},
            {"op": "triage_list", "status": "great"},
            {"op": "triage_retry", "id": 99},
        ):
            with self.subTest(request=request):
                self.assertFalse(service.handle(request)["ok"])

    def test_triage_log_is_pruned_after_the_retention_time(self):
        service = self.make_service(retention=None)
        old = self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.old", "status": "silent"})["id"]
        service.store._write("UPDATE triage SET created=? WHERE id=?", (time.time() - 31 * 86400, old))
        recent = self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.new", "status": "silent"})["id"]
        self.assertEqual([item["id"] for item in self.call(service, op="triage_list")["entries"]], [recent])
        self.assertEqual(self.call(service, op="triage_list")["retention_days"], 30)

    def test_triage_retention_zero_keeps_everything(self):
        service = self.make_service(retention=0)
        old = self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.old", "status": "silent"})["id"]
        service.store._write("UPDATE triage SET created=? WHERE id=?", (time.time() - 400 * 86400, old))
        self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.new", "status": "silent"})
        self.assertEqual(self.call(service, op="triage_list")["count"], 2)

    def test_triage_retry_queues_a_new_event_for_mail_in_the_index(self):
        service = self.start()
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        identifier = self.call(service, op="triage_record", entry={"account": "uni", "mail_id": homework["id"], "status": "silent"})["id"]
        self.call(service, op="triage_retry", id=identifier)
        [event] = self.call(service, op="events")["events"]
        self.assertEqual((event["kind"], event["mail_id"], event["account"]), ("mail.new", homework["id"], "uni"))
        gone = self.call(service, op="triage_record", entry={"account": "uni", "mail_id": "uni.0000000000000000", "status": "silent"})["id"]
        refused = service.handle({"op": "triage_retry", "id": gone})
        self.assertFalse(refused["ok"])
        self.assertIn("sync window", refused["error"])

    def test_folders_rejects_an_unknown_account(self):
        service = self.start()
        response = service.handle({"op": "folders", "account": "nope"})
        self.assertFalse(response["ok"])
        self.assertIn("unknown account", response["error"])

    def test_archive_moves_the_message_with_uid_move(self):
        service = self.start(archive_folder="Archive")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        before = len(self.box.commands)
        result = self.call(service, op="archive", ids=[homework["id"]])
        self.assertEqual(result["results"][homework["id"]], {"ok": True, "folder": "Archive"})
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("UID MOVE") for command in commands))
        self.assertFalse(any(command.startswith(("UID COPY", "UID STORE", "UID EXPUNGE", "CLOSE", "EXPUNGE")) for command in commands))
        # The message left the synced folder and left the folder mailing list.
        self.assertIsNone(service.store.get(homework["id"]))
        self.assertNotIn(2, {m.uid for m in self.box.messages})
        self.assertIn(2, {m.uid for m in self.box.folders["Archive"]})

    def test_archive_falls_back_to_copy_store_expunge_without_move(self):
        self.box.capabilities = "IMAP4rev1 IDLE UIDPLUS UNSELECT AUTH=XOAUTH2 AUTH=PLAIN"
        service = self.start(archive_folder="Archive")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        before = len(self.box.commands)
        result = self.call(service, op="archive", ids=[homework["id"]])
        self.assertEqual(result["results"][homework["id"]], {"ok": True, "folder": "Archive"})
        commands = [command.split(" ", 1)[1].upper() for command in self.box.commands[before:]]
        self.assertTrue(any(command.startswith("UID COPY") for command in commands))
        self.assertTrue(any(command.startswith("UID STORE") and "\\DELETED" in command for command in commands))
        self.assertTrue(any(command.startswith("UID EXPUNGE") for command in commands))
        self.assertFalse(any(command.startswith(("UID MOVE", "CLOSE")) or command == "EXPUNGE" for command in commands))
        self.assertNotIn(2, {m.uid for m in self.box.messages})
        self.assertIn(2, {m.uid for m in self.box.folders["Archive"]})

    def test_archive_reports_when_the_server_supports_neither_move_nor_uidplus(self):
        self.box.capabilities = "IMAP4rev1 IDLE UNSELECT AUTH=XOAUTH2 AUTH=PLAIN"
        service = self.start(archive_folder="Archive")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        response = service.handle({"op": "archive", "ids": [homework["id"]]})
        self.assertFalse(response["ok"])
        self.assertIn("neither MOVE nor UIDPLUS", response["error"])
        # Nothing moved, and the message stays in the index.
        self.assertIn(2, {m.uid for m in self.box.messages})
        self.assertIsNotNone(service.store.get(homework["id"]))

    def test_archive_without_an_archive_folder_is_refused(self):
        # The imap provider has no default archive_folder, unlike microsoft
        # and google.
        self.box.password = "app-password"
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(
            signed_in=False, provider="imap", auth="password", host="127.0.0.1", password_file=str(password),
        )
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the first sync")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        response = service.handle({"op": "archive", "ids": [homework["id"]]})
        self.assertFalse(response["ok"])
        self.assertIn("no archive folder is configured", response["error"])

    def test_archive_already_in_the_archive_folder_is_refused(self):
        service = self.start(archive_folder="INBOX")
        homework = self.call(service, op="list", query="Homework")["messages"][0]
        response = service.handle({"op": "archive", "ids": [homework["id"]]})
        self.assertFalse(response["ok"])
        self.assertIn("already in the archive folder", response["error"])

    def test_revoked_sign_in_waits_for_a_new_login(self):
        self.tokens.revoked = True
        service = self.make_service()
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "auth_required", message="auth_required")
        events = service.store.pending_events()
        self.assertEqual([event["kind"] for event in events], ["mail.auth"])
        self.assertIn("invalid_grant", events[0]["detail"])
        # One event only, even when the worker checks again.
        time.sleep(0.5)
        self.assertEqual(len(service.store.pending_events()), 1)

        self.tokens.revoked = False
        begin = self.call(service, op="auth_begin", account="uni")
        self.assertIn("client_id=9e5f94bc-e8a4-4e73-b8be-63364c29d753", begin["url"])
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(begin["url"]).query)["state"][0]
        wrong = service.handle({"op": "auth_finish", "account": "uni", "redirect": "https://localhost/?code=good-code&state=x"})
        self.assertFalse(wrong["ok"])
        self.call(service, op="auth_finish", account="uni", redirect=f"https://localhost/?code=good-code&state={state}")
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the sync after the new sign-in")
        token_file = self.root / "state" / "tokens" / "uni.json"
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_unexpected_error_does_not_stop_the_worker(self):
        service = self.make_service()
        account = service.accounts["uni"]
        calls = []
        original = account.sync

        def broken_once(imap):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return original(imap)

        account.sync = broken_once
        with mock.patch("hermes_mail.account.log"):
            service.start()
            wait_for(lambda: account.status == "error", message="the error state")
            self.assertIn("database is locked", account.error)
            account.wake()
            wait_for(lambda: account.status == "idle", message="the recovery")

    def test_uidvalidity_change_starts_a_new_baseline(self):
        service = self.start()
        old_ids = {m["id"] for m in self.call(service, op="list")["messages"]}
        self.box.uidvalidity = 2
        account = service.accounts["uni"]
        imap = account.connect()
        try:
            account.sync(imap)
        finally:
            imap.logout()
        new_ids = {m["id"] for m in self.call(service, op="list")["messages"]}
        self.assertEqual(len(new_ids), 3)
        self.assertFalse(old_ids & new_ids)
        self.assertEqual(service.store.pending_events(), [])

    def test_password_account(self):
        self.box.password = "app-password"
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(signed_in=False, provider="google", auth="password", password_file=str(password))
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "idle", message="a password sign-in")
        self.assertEqual(self.call(service, op="list")["count"], 3)

    def fail(self, service: Service, **request) -> str:
        response = service.handle(request)
        self.assertFalse(response["ok"], response)
        return response["error"]

    def test_settings_change_an_account_without_a_restart(self):
        service = self.start()
        worker = service.accounts["uni"]
        settings = self.call(service, op="settings")
        self.assertTrue(settings["editable"])
        [uni] = settings["accounts"]
        self.assertEqual((uni["source"], uni["changed"], uni["settings"]["sync_days"]), ("base", False, 7))
        values = {**uni["settings"], "sync_days": 14}
        self.call(service, op="settings_save", account="uni", settings=values)
        self.assertIsNot(service.accounts["uni"], worker)
        self.assertEqual(service.accounts["uni"].cfg.sync_days, 14)
        self.assertTrue(worker.stop.is_set())
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the sync with the new settings")
        # The address did not change, so the sign-in and the index stay.
        self.assertEqual(self.call(service, op="list")["count"], 3)
        [uni] = self.call(service, op="settings")["accounts"]
        self.assertEqual((uni["changed"], uni["settings"]["sync_days"], uni["base"]["sync_days"]), (True, 14, 7))
        # The change stays after a restart.
        service.stop()
        self.service = Service(config.parse(self.raw), imap_factory=service.imap_factory)
        self.assertEqual(self.service.accounts["uni"].cfg.sync_days, 14)
        self.call(self.service, op="settings_reset", account="uni")
        self.assertEqual(self.service.accounts["uni"].cfg.sync_days, 7)
        self.assertFalse(self.call(self.service, op="settings")["accounts"][0]["changed"])

    def test_settings_add_and_remove_a_password_account(self):
        service = self.start()
        self.box.password = "app-password"
        values = {"provider": "imap", "address": USER, "host": "127.0.0.1", "folders": ["INBOX"]}
        self.assertIn("password_file", self.fail(service, op="settings_save", account="other", settings=values))
        self.call(service, op="settings_save", account="other", settings=values, password="app-password")
        stored = self.root / "state" / "passwords" / "other"
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(service.accounts["other"].cfg.password_file, str(stored))
        wait_for(lambda: service.accounts["other"].status == "idle", message="the new account")
        self.assertEqual(self.call(service, op="list", account="other")["count"], 3)
        self.assertEqual(self.call(service, op="settings")["accounts"][0]["password"], "dashboard")
        self.call(service, op="settings_delete", account="other")
        self.assertNotIn("other", service.accounts)
        self.assertFalse(stored.exists())
        self.assertEqual(self.call(service, op="list", account="")["count"], 3)
        # A base account is only marked as removed, and it can come back.
        self.call(service, op="settings_delete", account="uni")
        self.assertEqual(service.accounts, {})
        [uni] = self.call(service, op="settings")["accounts"]
        self.assertTrue(uni["removed"])
        self.call(service, op="settings_reset", account="uni")
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the restored account")

    def test_settings_cannot_send_secrets_to_another_server(self):
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(provider="microsoft")
        values = self.call(service, op="settings")["accounts"][0]["settings"]
        error = self.fail(service, op="settings_save", account="uni", settings={**values, "host": "evil.example"})
        self.assertIn("host of its provider", error)
        self.fail(service, op="settings_save", account="uni", settings={**values, "password_file": str(password)})
        self.fail(service, op="settings_save", account="Bad name", settings=values)
        self.fail(service, op="settings_save", account="uni", settings={**values, "port": 0})
        self.fail(service, op="settings_save", account="uni", settings={**values, "folders": []})
        self.assertEqual(service.accounts["uni"].host, "outlook.office365.com")

    def test_settings_keep_a_base_password_only_for_the_same_server(self):
        password = self.root / "password"
        password.write_text("app-password\n")
        service = self.make_service(signed_in=False, provider="imap", host="127.0.0.1", password_file=str(password))
        values = self.call(service, op="settings")["accounts"][0]["settings"]
        self.call(service, op="settings_save", account="uni", settings={**values, "sync_days": 3})
        self.assertEqual(service.accounts["uni"].cfg.password_file, str(password))
        error = self.fail(service, op="settings_save", account="uni", settings={**values, "host": "evil.example"})
        self.assertIn("password_file", error)
        self.assertEqual(service.accounts["uni"].cfg.host, "127.0.0.1")

    def test_settings_can_be_turned_off(self):
        service = self.make_service(web_settings=False)
        values = self.call(service, op="settings")["accounts"][0]["settings"]
        self.assertFalse(self.call(service, op="settings")["editable"])
        self.assertIn("web_settings", self.fail(service, op="settings_save", account="uni", settings=values))

    def test_socket_client(self):
        service = self.start()
        self.server = SocketServer(self.root / "mail.sock", service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        client = Client(str(self.root / "mail.sock"))
        status = client.status()["accounts"][0]
        self.assertEqual((status["name"], status["status"], status["messages"], status["unread"]), ("uni", "idle", 3, 2))
        self.assertEqual(client.list(unread=True)["count"], 2)
        self.assertEqual(client.list(since="2d")["count"], 2)
        with self.assertRaises(MailServiceError) as raised:
            client.show("uni.missing")
        self.assertIn("message not found", str(raised.exception))
        self.assertEqual((self.root / "mail.sock").stat().st_mode & 0o777, 0o660)
        self.assertNotIn("refresh", json.dumps(client.status()))

    def test_client_reports_a_stopped_service(self):
        with self.assertRaises(MailServiceError) as raised:
            Client(str(self.root / "missing.sock")).status()
        self.assertIn("not running", str(raised.exception))



if __name__ == "__main__":
    unittest.main()
