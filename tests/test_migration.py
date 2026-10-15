"""Tests for the hermes-mail to uni-mail migration and for the index upgrade."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import imaplib
import json
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))

from fake_imap import FakeIMAPServer, FakeTokenEndpoint, Mailbox, Message  # noqa: E402
from test_service import USER, sample_messages, wait_for  # noqa: E402
from uni_mail import auth, config  # noqa: E402
from uni_mail.client import Client  # noqa: E402
from uni_mail.dispatch import config as dispatch_config  # noqa: E402
from uni_mail.dispatch.runner import Dispatcher  # noqa: E402
from uni_mail.migrate import OLD_SOCKET, Migration, run  # noqa: E402
from uni_mail.server import Service, SocketServer, legacy_daemon_running, lock_state  # noqa: E402
from uni_mail.store import SCHEMA_VERSION, Store, mail_id  # noqa: E402

# The index of hermes-mail, PRAGMA user_version 0.
OLD_SCHEMA = """
CREATE TABLE folders (account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, baseline INTEGER NOT NULL DEFAULT 0, synced REAL NOT NULL DEFAULT 0, PRIMARY KEY (account, folder));
CREATE TABLE messages (id TEXT PRIMARY KEY, account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL, message_id TEXT NOT NULL DEFAULT '', date TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '', recipients TEXT NOT NULL DEFAULT '', cc TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', read INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0, parts TEXT NOT NULL DEFAULT '[]', preview TEXT NOT NULL DEFAULT '', indexed REAL NOT NULL, UNIQUE (account, folder, uidvalidity, uid));
CREATE INDEX messages_date ON messages (date);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, account TEXT NOT NULL, mail_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE triage (id INTEGER PRIMARY KEY AUTOINCREMENT, event_seq INTEGER UNIQUE, run_id TEXT NOT NULL DEFAULT '', account TEXT NOT NULL, mail_id TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '', mail_date TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, decision TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', actions TEXT NOT NULL DEFAULT '[]', attachments TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, finished REAL NOT NULL DEFAULT 0);
"""


def build_old_index(directory: Path) -> list[str]:
    """Write an index in the hermes-mail format and return the mail IDs in it."""
    directory.mkdir(parents=True, exist_ok=True)
    ids = []
    with closing(sqlite3.connect(directory / "index.sqlite3", isolation_level=None)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(OLD_SCHEMA)
        db.execute("INSERT INTO folders (account, folder, uidvalidity, baseline, synced) VALUES ('uni', 'INBOX', 1, 1, 1000.5)")
        for uid, subject in ((2, "Homework 4"), (3, "Exam moved")):
            identifier = mail_id("uni", "INBOX", 1, uid)
            ids.append(identifier)
            db.execute(
                "INSERT INTO messages (id, account, folder, uidvalidity, uid, message_id, date, sender, subject, preview, indexed) "
                "VALUES (?, 'uni', 'INBOX', 1, ?, ?, '2026-10-01T10:00:00+00:00', 'Sender <s@example.com>', ?, 'preview text', 1000.0)",
                (identifier, uid, f"<{uid}@example.com>", subject),
            )
        db.execute("INSERT INTO events (kind, account, mail_id, detail, created, done) VALUES ('mail.new', 'uni', ?, '', 1001.0, 1)", (ids[0],))
        db.execute("INSERT INTO events (kind, account, mail_id, detail, created, done) VALUES ('mail.error', 'uni', '', 'boom', 1002.0, 1)")
        db.execute("INSERT INTO events (kind, account, mail_id, detail, created, done) VALUES ('mail.new', 'uni', ?, '', 1003.0, 0)", (ids[1],))
        db.execute(
            "INSERT INTO triage (event_seq, run_id, account, mail_id, status, decision, created) VALUES (1, 'run-1', 'uni', ?, 'done', 'notify', 1001.5)",
            (ids[0],),
        )
    return ids


def tree_hashes(*roots: Path) -> dict[str, tuple[str, int]]:
    """Every file below the roots with its SHA-256 and its mode."""
    found = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                found[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode))
    return found


def rows(path: Path, sql: str) -> list[tuple]:
    # immutable: reading must not create -shm files next to the database.
    with closing(sqlite3.connect(f"file:{path}?immutable=1", uri=True)) as db:
        return db.execute(sql).fetchall()


class Layout:
    """An old hermes-mail installation and the places of the new one, all below one temporary directory."""

    def __init__(self, root: Path, socket_path: str = OLD_SOCKET):
        self.root = root
        self.old_etc = root / "etc" / "hermes-mail"
        self.old_state = root / "var" / "lib" / "hermes-mail"
        self.new_etc = root / "etc" / "uni-mail"
        self.new_state = root / "var" / "lib" / "uni-mail"
        self.from_config = self.old_etc / "config.json"
        self.to_config = self.new_etc / "config.json"
        self.socket_path = socket_path

    def build(self, *, index: bool = True) -> None:
        (self.old_etc / "passwords").mkdir(parents=True)
        (self.old_etc / "passwords" / "self").write_text("app-password\n")
        (self.old_etc / "policy.md").write_text("Tell me about exams.\n")
        (self.old_state / "tokens").mkdir(parents=True)
        (self.old_state / "passwords").mkdir()
        (self.old_state / "cache" / "uni.0123").mkdir(parents=True)
        (self.old_state / "cache" / "uni.0123" / "part").write_bytes(b"cached part")
        token = self.old_state / "tokens" / "uni.json"
        token.write_text(json.dumps({"refresh_token": "refresh-1", "obtained": 1234.5}))
        token.chmod(0o600)
        stored = self.old_state / "passwords" / "web"
        stored.write_text("web-password\n")
        stored.chmod(0o600)
        (self.old_state / "settings.json").write_text(json.dumps({"accounts": {"uni": {"sync_days": 14}}}))
        if index:
            build_old_index(self.old_state)
        self.write_config()

    def raw(self) -> dict:
        return {
            "state_dir": str(self.old_state),
            "socket": self.socket_path,
            "web_settings": True,
            "triage_retention_days": 30,
            "export_dir": str(self.old_state / "exports"),
            "accounts": {
                "uni": {"provider": "microsoft", "address": USER, "notify": {
                    "mode": "triage", "target": "discord:1", "policy_file": str(self.old_etc / "policy.md"), "mark_read_silent": True}},
                "self": {"provider": "imap", "address": "u@x", "host": "mail.x",
                         "password_file": str(self.old_etc / "passwords" / "self"), "notify": {"mode": "none"}},
                "other": {"provider": "imap", "address": "o@x", "host": "mail.o", "password_file": "/srv/outside/password"},
            },
        }

    def write_config(self, raw: dict | None = None) -> None:
        self.from_config.parent.mkdir(parents=True, exist_ok=True)
        self.from_config.write_text(json.dumps(raw or self.raw(), indent=2))

    def migration(self, **kwargs) -> Migration:
        return Migration(self.from_config, self.to_config, self.new_state, owner="uni-mail", **kwargs)

    def old_files(self) -> dict[str, tuple[str, int]]:
        return tree_hashes(self.old_etc, self.old_state)


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        # No test depends on the units or the sockets of the machine, and none changes an owner.
        for target, value in (("uni_mail.migrate._active_units", []), ("uni_mail.migrate.legacy_daemon_running", False)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        owner = mock.patch("uni_mail.migrate.os.geteuid", return_value=1000)
        owner.start()
        self.addCleanup(owner.stop)

    def layout(self, **kwargs) -> Layout:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Layout(Path(temporary.name), **kwargs)


class IndexUpgradeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        self.ids = build_old_index(self.state)

    def open(self) -> Store:
        store = Store(self.state)
        self.addCleanup(lambda: store._db.close() if _open(store) else None)
        return store

    def test_old_index_gets_stable_unique_event_ids(self):
        store = self.open()
        self.assertEqual(store._db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 2)
        events = store.events_after(0)
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertEqual([event["done"] for event in events], [1, 1, 0])
        identifiers = [event["event_id"] for event in events]
        self.assertTrue(all(identifiers), identifiers)
        self.assertEqual(len(set(identifiers)), 3)
        self.assertEqual([event["data"] for event in events], [{}, {}, {}])
        self.assertEqual(events[1]["detail"], "boom")
        self.assertEqual(store.event_head(), {"first": 1, "last": 3, "first_pending": 3})
        self.assertEqual(store.get(self.ids[0])["subject"], "Homework 4")
        # The triage log stays in the file, unused.
        triage = store._db.execute("SELECT run_id, status, decision, mail_id FROM triage").fetchall()
        self.assertEqual([tuple(row) for row in triage], [("run-1", "done", "notify", self.ids[0])])
        store.close()

        again = self.open()
        self.assertEqual([event["event_id"] for event in again.events_after(0)], identifiers)
        self.assertEqual(again._db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_old_style_insert_still_works_after_the_upgrade(self):
        store = self.open()
        store._db.execute("INSERT INTO events (kind, account, mail_id, detail, created) VALUES ('mail.error', 'uni', '', 'old code', 5.0)")
        store._db.execute("INSERT INTO events (kind, account, mail_id, detail, created) VALUES ('mail.error', 'uni', '', 'old code 2', 6.0)")
        self.assertEqual([event["detail"] for event in store.events_after(3)], ["old code", "old code 2"])
        # The new code still appends its own events with an ID.
        added = store.add_event("mail.error", "uni", detail="new", data={"detail": "new"})
        self.assertTrue(added["event_id"])
        self.assertEqual(added["data"], {"detail": "new"})

    def test_events_written_by_the_old_code_in_between_get_ids_at_the_next_start(self):
        # Rollback to hermes-mail and forward again: the events that hermes-mail wrote have no event ID.
        store = self.open()
        before = [event["event_id"] for event in store.events_after(0)]
        store._db.execute("INSERT INTO events (kind, account, mail_id, detail, created) VALUES ('mail.error', 'uni', '', 'rolled back', 7.0)")
        store.close()
        again = self.open()
        events = again.events_after(0)
        self.assertEqual([event["event_id"] for event in events[:3]], before)
        self.assertEqual(len(events), 4)
        self.assertTrue(events[3]["event_id"])
        self.assertEqual(len({event["event_id"] for event in events}), 4)


def _open(store: Store) -> bool:
    try:
        store._db.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


class DryRunTest(MigrationTestCase):
    def test_dry_run_reports_a_plan_and_writes_nothing(self):
        layout = self.layout()
        layout.build()
        before = layout.old_files()
        migration = layout.migration()
        migration.check()
        self.assertEqual(migration.problems, [])
        report = migration.report(applied=False)
        self.assertFalse(report["applied"])
        self.assertTrue(report["plan"])
        self.assertNotIn("actions", report)
        self.assertNotIn("problems", report)
        plan = "\n".join(report["plan"])
        self.assertIn(str(layout.to_config), plan)
        self.assertIn("index.sqlite3", plan)
        self.assertEqual(report["removed_settings"]["notify"].keys(), {"uni", "self"})
        self.assertFalse(layout.to_config.exists())
        self.assertFalse(layout.new_state.exists())
        self.assertFalse(layout.new_etc.exists())
        self.assertEqual(layout.old_files(), before)

    def test_run_without_apply_is_a_dry_run(self):
        layout = self.layout()
        layout.build()
        result = run(self.args(layout, apply=False))
        self.assertFalse(result["applied"])
        self.assertTrue(result["plan"])
        self.assertNotIn("ok", result)
        self.assertFalse(layout.to_config.exists())
        self.assertFalse(layout.new_state.exists())

    @staticmethod
    def args(layout: Layout, **kwargs) -> argparse.Namespace:
        values = {"from_config": str(layout.from_config), "to_config": str(layout.to_config), "state_dir": str(layout.new_state),
                  "owner": "uni-mail", "with_cache": False, "apply": False}
        return argparse.Namespace(**{**values, **kwargs})


class ApplyTest(MigrationTestCase):
    def test_apply_copies_everything_and_leaves_the_old_installation_alone(self):
        layout = self.layout()
        layout.build()
        before = layout.old_files()
        migration = layout.migration()
        migration.check()
        self.assertEqual(migration.problems, [])
        migration.apply()
        report = migration.report(applied=True)
        self.assertTrue(report["applied"])
        self.assertTrue(report["actions"])
        self.assertEqual(report["removed_settings"]["notify"].keys(), {"uni", "self"})
        self.assertEqual(report["removed_settings"]["triage_retention_days"], 30)
        self.assertEqual(report["removed_settings"]["notify"]["uni"]["mode"], "triage")

        raw = json.loads(layout.to_config.read_text())
        self.assertNotIn("triage_retention_days", raw)
        for account in raw["accounts"].values():
            self.assertNotIn("notify", account)
        self.assertEqual(raw["state_dir"], str(layout.new_state))
        self.assertEqual(raw["socket"], "/run/uni-mail/mail.sock")
        self.assertEqual(raw["export_dir"], str(layout.new_state / "exports"))
        new_password = layout.new_etc / "passwords" / "self"
        self.assertEqual(raw["accounts"]["self"]["password_file"], str(new_password))
        self.assertEqual(new_password.read_bytes(), (layout.old_etc / "passwords" / "self").read_bytes())
        self.assertEqual(raw["accounts"]["other"]["password_file"], "/srv/outside/password")
        self.assertEqual(raw["accounts"]["uni"]["address"], USER)
        # The policy file only fed the old triage; nothing refers to it any more, and it is not copied.
        self.assertFalse((layout.new_etc / "policy.md").exists())
        parsed = config.load(layout.to_config)
        self.assertEqual(parsed.state_dir, layout.new_state)
        self.assertEqual(sorted(parsed.accounts), ["other", "self", "uni"])

        old_token = layout.old_state / "tokens" / "uni.json"
        new_token = layout.new_state / "tokens" / "uni.json"
        self.assertEqual(new_token.read_bytes(), old_token.read_bytes())
        self.assertEqual(stat.S_IMODE(new_token.stat().st_mode), 0o600)
        self.assertEqual((layout.new_state / "passwords" / "web").read_bytes(), b"web-password\n")
        self.assertEqual(stat.S_IMODE((layout.new_state / "passwords" / "web").stat().st_mode), 0o600)
        self.assertEqual((layout.new_state / "settings.json").read_bytes(), (layout.old_state / "settings.json").read_bytes())
        self.assertEqual(stat.S_IMODE((layout.new_state / "settings.json").stat().st_mode), 0o600)
        # The cache is fetched again when needed.
        self.assertFalse((layout.new_state / "cache").exists())

        old_index, new_index = layout.old_state / "index.sqlite3", layout.new_state / "index.sqlite3"
        for sql in (
            "SELECT * FROM messages ORDER BY id",
            "SELECT seq, kind, account, mail_id, detail, created, done FROM events ORDER BY seq",
            "SELECT * FROM folders",
            "SELECT * FROM triage",
        ):
            self.assertEqual(rows(new_index, sql), rows(old_index, sql), sql)
        self.assertEqual(len(rows(new_index, "SELECT 1 FROM events")), 3)
        self.assertEqual(rows(new_index, "PRAGMA user_version"), [(0,)])

        marker = json.loads((layout.new_state / "MIGRATED-FROM-HERMES-MAIL.json").read_text())
        self.assertEqual((marker["from_config"], marker["from_state"]), (str(layout.from_config), str(layout.old_state)))

        self.assertEqual(layout.old_files(), before)

    def test_apply_with_cache_copies_the_cache(self):
        layout = self.layout()
        layout.build()
        migration = layout.migration(with_cache=True)
        migration.check()
        self.assertEqual(migration.problems, [])
        migration.apply()
        self.assertEqual((layout.new_state / "cache" / "uni.0123" / "part").read_bytes(), b"cached part")

    def test_a_socket_that_is_not_the_default_is_kept(self):
        layout = self.layout(socket_path="/run/custom/mail.sock")
        layout.build()
        migration = layout.migration()
        migration.check()
        migration.apply()
        self.assertEqual(json.loads(layout.to_config.read_text())["socket"], "/run/custom/mail.sock")


class RefusalTest(MigrationTestCase):
    def problems(self, layout: Layout) -> list[str]:
        migration = layout.migration()
        migration.check()
        return migration.problems

    def test_existing_new_config(self):
        layout = self.layout()
        layout.build()
        layout.to_config.parent.mkdir(parents=True)
        layout.to_config.write_text('{"mine": true}')
        problems = self.problems(layout)
        self.assertEqual(len(problems), 1)
        self.assertIn(str(layout.to_config), problems[0])
        self.assertEqual(layout.to_config.read_text(), '{"mine": true}')

    def test_state_that_exists(self):
        for name, create in (
            ("index.sqlite3", lambda state: (state / "index.sqlite3").write_bytes(b"mine")),
            ("tokens", lambda state: ((state / "tokens").mkdir(), (state / "tokens" / "uni.json").write_text("mine"))),
        ):
            with self.subTest(name):
                layout = self.layout()
                layout.build()
                layout.new_state.mkdir(parents=True)
                create(layout.new_state)
                before = tree_hashes(layout.new_state)
                problems = self.problems(layout)
                self.assertEqual(len(problems), 1)
                self.assertIn(name, problems[0])
                self.assertIn("never overwrites", problems[0])
                result = run(DryRunTest.args(layout, apply=True))
                self.assertIs(result["ok"], False)
                self.assertFalse(result["applied"])
                self.assertEqual(tree_hashes(layout.new_state), before)
                self.assertFalse(layout.to_config.exists())

    def test_empty_state_directories_are_fine(self):
        layout = self.layout()
        layout.build()
        (layout.new_state / "tokens").mkdir(parents=True)
        self.assertEqual(self.problems(layout), [])

    def test_the_old_state_directory_as_the_new_one(self):
        layout = self.layout()
        layout.build()
        before = layout.old_files()
        migration = Migration(layout.from_config, layout.to_config, layout.old_state)
        migration.check()
        self.assertTrue(any("is the hermes-mail one" in problem for problem in migration.problems), migration.problems)
        result = run(DryRunTest.args(layout, apply=True, state_dir=str(layout.old_state)))
        self.assertIs(result["ok"], False)
        self.assertEqual(layout.old_files(), before)
        self.assertFalse(layout.to_config.exists())

    def test_no_index_to_migrate(self):
        layout = self.layout()
        layout.build(index=False)
        problems = self.problems(layout)
        self.assertEqual(len(problems), 1)
        self.assertIn("nothing to migrate", problems[0])

    def test_missing_password_file(self):
        layout = self.layout()
        layout.build()
        (layout.old_etc / "passwords" / "self").unlink()
        problems = self.problems(layout)
        self.assertEqual(len(problems), 1)
        self.assertIn("password file", problems[0])

    def test_a_live_legacy_socket_blocks_and_a_stale_one_does_not(self):
        layout = self.layout()
        layout.socket_path = str(layout.root / "run" / "mail.sock")
        layout.build()
        path = Path(layout.socket_path)
        path.parent.mkdir()
        # Here the real check runs.
        with mock.patch("uni_mail.migrate.legacy_daemon_running", legacy_daemon_running):
            self.assertFalse(legacy_daemon_running(path))
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                listener.listen()
                self.assertTrue(legacy_daemon_running(path))
                problems = self.problems(layout)
                self.assertEqual(len(problems), 1)
                self.assertIn("daemon answers", problems[0])
                result = run(DryRunTest.args(layout, apply=True))
                self.assertIs(result["ok"], False)
                self.assertFalse(result["applied"])
                self.assertFalse(layout.to_config.exists())
                self.assertFalse(layout.new_state.exists())
            finally:
                listener.close()
            # The file stays after the listener is gone: a stale socket.
            self.assertTrue(path.is_socket())
            self.assertFalse(legacy_daemon_running(path))
            self.assertEqual(self.problems(layout), [])
            result = run(DryRunTest.args(layout, apply=True))
            self.assertTrue(result["applied"])
            self.assertNotIn("ok", result)
            self.assertTrue(layout.to_config.exists())

    def test_an_active_legacy_unit_blocks(self):
        layout = self.layout()
        layout.build()
        with mock.patch("uni_mail.migrate._active_units", return_value=["hermes-mail.service"]):
            problems = self.problems(layout)
        self.assertEqual(len(problems), 1)
        self.assertIn("hermes-mail.service is active", problems[0])

    def test_run_with_problems_does_not_apply(self):
        layout = self.layout()
        layout.build()
        layout.to_config.parent.mkdir(parents=True)
        layout.to_config.write_text("{}")
        with mock.patch.object(Migration, "apply", side_effect=AssertionError("applied")):
            result = run(DryRunTest.args(layout, apply=True))
        self.assertIs(result["ok"], False)
        self.assertFalse(result["applied"])
        self.assertTrue(result["problems"])
        self.assertFalse(layout.new_state.exists())


class LockTest(unittest.TestCase):
    def test_second_lock_fails_until_the_first_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            first = lock_state(state)
            try:
                with self.assertRaises(config.ConfigError) as raised:
                    lock_state(state)
                self.assertIn("another uni-maild", str(raised.exception))
            finally:
                first.close()
            second = lock_state(state)
            second.close()


class EndToEndTest(MigrationTestCase):
    def setUp(self):
        super().setUp()
        self.layout_ = self.layout()
        self.root = self.layout_.root
        # A short path: Unix socket names are limited to about 100 bytes.
        self.layout_.socket_path = str(self.root / "run" / "mail.sock")
        self.box = Mailbox(user=USER, token="good-access", messages=sample_messages())
        self.imap = FakeIMAPServer(self.box)
        self.tokens = FakeTokenEndpoint(code="good-code", access_token="good-access")
        provider = dataclasses.replace(auth.PROVIDERS["microsoft"], token_endpoint=self.tokens.url)
        patcher = mock.patch.dict(auth.PROVIDERS, {"microsoft": provider})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.services: list[Service] = []
        self.server = None
        self.dispatcher = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.dispatcher:
            self.dispatcher.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        for service in self.services:
            self.stop(service)
        self.imap.close()
        self.tokens.close()

    def factory(self):
        port = self.imap.port
        return lambda host, _: imaplib.IMAP4("127.0.0.1", port, timeout=10)

    def stop(self, service: Service) -> None:
        service.stop()
        self.box.wake.set()  # ends the IDLE of the workers
        for thread in service.threads:
            thread.join(10)
            self.assertFalse(thread.is_alive(), thread.name)
        if _open(service.store):
            service.store.close()

    def test_migrated_installation_resumes_without_sign_in_or_duplicate_events(self):
        layout = self.layout_
        # The "old" installation: a real daemon indexes the mailbox, then the index looks like the one of hermes-mail.
        raw = layout.raw()
        raw["accounts"] = {"uni": raw["accounts"]["uni"]}
        layout.write_config(raw)
        old_cfg = config.parse(raw)
        auth.TokenStore(old_cfg.state_dir / "tokens").save("uni", {"refresh_token": "refresh-1", "obtained": time.time()})
        old = Service(old_cfg, imap_factory=self.factory())
        self.services.append(old)
        old.start()
        wait_for(lambda: old.accounts["uni"].status == "idle", message="the first sync of the old installation")
        self.stop(old)
        rotated = json.loads((layout.old_state / "tokens" / "uni.json").read_text())["refresh_token"]
        self.assertNotEqual(rotated, "refresh-1")
        homework = mail_id("uni", "INBOX", 1, 2)
        with closing(sqlite3.connect(layout.old_state / "index.sqlite3", isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT subject FROM messages WHERE id=?", (homework,)).fetchone()[0], "Homework 4")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            db.execute("PRAGMA user_version=0")
            db.execute("INSERT INTO events (kind, account, mail_id, detail, created) VALUES ('mail.new', 'uni', ?, '', ?)",
                       (homework, time.time()))
        before = layout.old_files()

        migration = layout.migration()
        migration.check()
        self.assertEqual(migration.problems, [])
        migration.apply()
        self.assertEqual(layout.old_files(), before)
        self.assertEqual((layout.new_state / "tokens" / "uni.json").read_bytes(), (layout.old_state / "tokens" / "uni.json").read_bytes())

        # The sign-in of the old installation is the only one the token endpoint accepts.
        self.tokens.valid_refresh = {rotated}
        requests_before = len(self.tokens.requests)
        cfg = config.load(layout.to_config)
        service = Service(cfg, imap_factory=self.factory())
        self.services.append(service)
        service.start()
        wait_for(lambda: service.accounts["uni"].status == "idle", message="the new daemon on the migrated state")
        new_requests = self.tokens.requests[requests_before:]
        self.assertTrue(new_requests)
        self.assertEqual({fields["grant_type"] for fields in new_requests}, {"refresh_token"})
        self.assertEqual(service.handle({"op": "list"})["count"], 3)

        # The baseline came with the index: no event for mail that was already there, only the waiting one.
        stored = service.store.events_after(0)
        self.assertEqual([(event["kind"], event["mail_id"], event["done"]) for event in stored], [("mail.new", homework, 0)])
        self.assertNotIn("mail.auth", [event["kind"] for event in stored])
        response = service.handle({"op": "events"})
        self.assertTrue(response["ok"])
        [legacy] = response["events"]
        self.assertRegex(legacy["event_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual((legacy["event_type"], legacy["account"], legacy["mail_id"]), ("mail.new", "uni", homework))
        self.assertEqual(legacy["data"]["subject"], "Homework 4")
        self.assertEqual(legacy["data"]["folder"], "INBOX")
        self.assertEqual(legacy["data"]["attachment_count"], 0)
        self.assertEqual(response["first_pending"], legacy["seq"])
        self.assertNotIn("preview", legacy["data"])

        # The dispatcher starts with its own empty database and delivers the waiting event once.
        self.server = SocketServer(cfg.socket, service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        sink = self.root / "delivered.jsonl"
        dispatch_cfg = dispatch_config.parse({
            "state_dir": str(self.root / "dispatch"),
            "destinations": {"file": {"type": "command", "argv": ["/bin/sh", "-c", 'cat >> "$0"; echo >> "$0"', str(sink)]}},
        })
        self.dispatcher = Dispatcher(dispatch_cfg, Client(str(cfg.socket)), owner="test")
        first = self.dispatcher.run_once()
        self.assertEqual((first["events"], first["attempts"]), (1, {"file": 1}))
        delivered = [json.loads(line) for line in sink.read_text().splitlines()]
        self.assertEqual([event["event_id"] for event in delivered], [legacy["event_id"]])
        self.assertEqual(delivered[0]["data"]["subject"], "Homework 4")
        self.assertEqual(service.store.event_head()["first_pending"], None)

        second = self.dispatcher.run_once()
        self.assertEqual((second["events"], second["attempts"]), (0, {"file": 0}))
        self.assertEqual(len(sink.read_text().splitlines()), 1)

        # New mail after the migration is one new event, delivered once.
        wait_for(lambda: self.box.idling.is_set(), message="IDLE")
        self.box.deliver(Message(5, date.today(), "Quiz tomorrow", body="Quiz on chapter 3.\r\n"))
        wait_for(lambda: len(service.store.events_after(legacy["seq"])) == 1, message="the event for the new mail")
        third = self.dispatcher.run_once()
        self.assertEqual((third["events"], third["attempts"]), (1, {"file": 1}))
        self.assertEqual(self.dispatcher.run_once()["attempts"], {"file": 0})
        delivered = [json.loads(line) for line in sink.read_text().splitlines()]
        self.assertEqual(len(delivered), 2)
        self.assertEqual(len({event["event_id"] for event in delivered}), 2)
        self.assertEqual(delivered[1]["data"]["subject"], "Quiz tomorrow")
        self.assertEqual(delivered[1]["seq"], legacy["seq"] + 1)
        self.assertEqual(self.tokens.requests[requests_before:][0]["refresh_token"], rotated)
        self.assertNotIn("authorization_code", {fields["grant_type"] for fields in self.tokens.requests})


if __name__ == "__main__":
    unittest.main()
