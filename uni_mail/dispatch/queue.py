"""The dispatcher's database: a copy of each event and one delivery per destination.

Every destination has its own delivery row, so its acknowledgement, retries
and failures do not touch the other destinations. A worker claims a row with
a lease; a row whose lease ran out (the worker crashed) can be claimed again.

Delivery states:
  pending    waiting for its next attempt (next_attempt)
  running    claimed by a worker until lease_until
  delivered  acknowledged by the destination
  dead       no more attempts: the attempts ran out or the destination refused
             for good. `uni-mail dispatch retry` puts it back to pending.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    account TEXT NOT NULL,
    mail_id TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    received REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS destinations (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    start_seq INTEGER NOT NULL,
    added REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events (event_id),
    destination TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_until REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    last_attempt REAL NOT NULL DEFAULT 0,
    last_code INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    last_output TEXT NOT NULL DEFAULT '',
    delivered REAL NOT NULL DEFAULT 0,
    UNIQUE (event_id, destination)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (destination, status, next_attempt);
"""
STATUSES = ("pending", "running", "delivered", "dead")
TEXT_LIMIT = 4000


def delivery_id(event_id: str, destination: str) -> str:
    """Stable for an event and a destination, so every retry has the same ID."""
    return "dlv_" + hashlib.sha256(f"{event_id}\0{destination}".encode()).hexdigest()[:24]


class Queue:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = state_dir / "dispatch.sqlite3"
        self._lock = threading.RLock()
        # Other dispatcher processes may share the file: wait for their writes.
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _transaction(self):
        queue = self

        class Transaction:
            def __enter__(self):
                queue._lock.acquire()
                queue._db.execute("BEGIN IMMEDIATE")
                return queue._db

            def __exit__(self, kind, value, traceback):
                try:
                    queue._db.execute("COMMIT" if kind is None else "ROLLBACK")
                finally:
                    queue._lock.release()
                return False

        return Transaction()

    # The cursor in the daemon's event log.

    def cursor(self) -> int | None:
        rows = self._rows("SELECT value FROM meta WHERE key='cursor'")
        return int(rows[0]["value"]) if rows else None

    def set_cursor(self, value: int) -> None:
        with self._transaction() as db:
            db.execute("INSERT INTO meta (key, value) VALUES ('cursor', ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                       (str(value),))

    # Destinations

    def sync_destinations(self, destinations: dict[str, str]) -> list[str]:
        """Record the configured destinations ({name: kind}). A new one gets the
        events after the current cursor, not the older ones. Return the new names."""
        added = []
        with self._transaction() as db:
            cursor = db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
            start = int(cursor["value"]) if cursor else 0
            known = {row["name"] for row in db.execute("SELECT name FROM destinations")}
            for name, kind in destinations.items():
                if name not in known:
                    db.execute("INSERT INTO destinations (name, kind, start_seq, added) VALUES (?, ?, ?, ?)",
                               (name, kind, start, time.time()))
                    added.append(name)
        return added

    def start_seq(self, name: str) -> int:
        rows = self._rows("SELECT start_seq FROM destinations WHERE name=?", (name,))
        return rows[0]["start_seq"] if rows else 0

    # Events in

    def ingest(self, events: list[dict[str, Any]], targets: dict[str, list[str]], cursor: int, body: Any) -> int:
        """Store the events with a pending delivery for each of their target
        destinations, and move the cursor, in one transaction. `body` turns an
        event into its bytes. Return the number of new deliveries."""
        now = time.time()
        created = 0
        with self._transaction() as db:
            for event in events:
                names = targets.get(event["event_id"]) or []
                if not names:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO events (event_id, seq, event_type, account, mail_id, body, received) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event["event_id"], event["seq"], event["event_type"], event["account"], event.get("mail_id") or "",
                     body(event).decode(), now),
                )
                for name in names:
                    created += db.execute(
                        "INSERT OR IGNORE INTO deliveries (delivery_id, event_id, destination, seq, status, next_attempt, created, updated) "
                        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                        (delivery_id(event["event_id"], name), event["event_id"], name, event["seq"], now, now, now),
                    ).rowcount
            db.execute("INSERT INTO meta (key, value) VALUES ('cursor', ?) ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                       (str(cursor),))
        return created

    # Work

    def claim(self, destination: str, owner: str, lease_seconds: float) -> dict[str, Any] | None:
        """Take the oldest due delivery of a destination, or one whose lease ran out."""
        now = time.time()
        with self._transaction() as db:
            row = db.execute(
                "SELECT delivery_id FROM deliveries WHERE destination=? AND "
                "((status='pending' AND next_attempt<=?) OR (status='running' AND lease_until<?)) ORDER BY seq LIMIT 1",
                (destination, now, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE deliveries SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1, last_attempt=?, updated=? "
                "WHERE delivery_id=?",
                (owner, now + lease_seconds, now, now, row["delivery_id"]),
            )
            return self._delivery(db, row["delivery_id"])

    def finish(self, delivery: dict[str, Any], owner: str, status: str, *, next_attempt: float = 0.0, code: int | None = None,
               error: str = "", output: str = "") -> bool:
        """Record the result of an attempt. Only the lease owner can; a worker
        that lost its lease changes nothing. Return whether the row changed."""
        now = time.time()
        with self._transaction() as db:
            return db.execute(
                "UPDATE deliveries SET status=?, next_attempt=?, lease_owner='', lease_until=0, updated=?, last_code=?, "
                "last_error=?, last_output=?, delivered=CASE WHEN ?='delivered' THEN ? ELSE delivered END "
                "WHERE delivery_id=? AND status='running' AND lease_owner=?",
                (status, next_attempt or now, now, code, error[:TEXT_LIMIT], output[:TEXT_LIMIT], status, now,
                 delivery["delivery_id"], owner),
            ).rowcount > 0

    def next_due(self, destination: str) -> float | None:
        row = self._rows(
            "SELECT MIN(CASE WHEN status='pending' THEN next_attempt ELSE lease_until END) AS due FROM deliveries "
            "WHERE destination=? AND status IN ('pending', 'running')", (destination,))[0]
        return row["due"]

    # Inspection and manual actions

    @staticmethod
    def _delivery(db: sqlite3.Connection, identifier: str) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT deliveries.*, events.body AS event_body, events.event_type, events.account, events.mail_id "
            "FROM deliveries JOIN events USING (event_id) WHERE delivery_id=?", (identifier,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["body"] = record.pop("event_body")
        record["event"] = json.loads(record["body"])
        return record

    def get(self, identifier: str) -> dict[str, Any] | None:
        """A delivery by its ID or a unique prefix of it."""
        with self._lock:
            record = self._delivery(self._db, identifier)
            if record is not None or not identifier:
                return record
            rows = self._db.execute("SELECT delivery_id FROM deliveries WHERE substr(delivery_id, 1, ?)=? LIMIT 2",
                                    (len(identifier), identifier)).fetchall()
            return self._delivery(self._db, rows[0]["delivery_id"]) if len(rows) == 1 else None

    def list(self, *, destination: str = "", status: str = "", event_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        clauses, args = [], []
        for column, value in (("destination", destination), ("status", status), ("event_id", event_id)):
            if value:
                clauses.append(f"deliveries.{column}=?")
                args.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(
            "SELECT delivery_id, destination, deliveries.event_id, events.event_type, events.account, events.mail_id, status, "
            f"attempts, next_attempt, last_attempt, last_code, last_error, created, delivered FROM deliveries JOIN events USING (event_id) "
            f"{where} ORDER BY deliveries.seq DESC, destination LIMIT ?", (*args, max(1, min(limit, 1000))))
        return [dict(row) for row in rows]

    def retry(self, identifier: str, force: bool = False) -> bool:
        """Put a dead (or, with force, a delivered) delivery back in the queue with a full set of attempts."""
        allowed = ("dead", "pending", "delivered") if force else ("dead", "pending")
        now = time.time()
        with self._transaction() as db:
            return db.execute(
                f"UPDATE deliveries SET status='pending', attempts=0, next_attempt=?, updated=? "
                f"WHERE delivery_id=? AND status IN ({', '.join('?' * len(allowed))})",
                (now, now, identifier, *allowed),
            ).rowcount > 0

    def add_deliveries(self, events: list[dict[str, Any]], destination: str, body: Any) -> int:
        """Queue older events for one destination (a replay). An event that it already has is skipped."""
        return self.ingest(events, {event["event_id"]: [destination] for event in events}, self.cursor() or 0, body)

    def stats(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in self._rows("SELECT name, kind, start_seq, added FROM destinations"):
            result[row["name"]] = {"kind": row["kind"], "start_seq": row["start_seq"], **{status: 0 for status in STATUSES},
                                   "oldest_pending": 0.0, "last_delivered": 0.0, "last_failure": 0.0, "last_error": ""}
        for row in self._rows("SELECT destination, status, COUNT(*) AS count FROM deliveries GROUP BY destination, status"):
            result.setdefault(row["destination"], {**{status: 0 for status in STATUSES}})[row["status"]] = row["count"]
        for row in self._rows(
            "SELECT destination, MIN(CASE WHEN status IN ('pending', 'running') THEN created END) AS oldest, "
            "MAX(delivered) AS delivered FROM deliveries GROUP BY destination"):
            entry = result[row["destination"]]
            entry["oldest_pending"] = row["oldest"] or 0.0
            entry["last_delivered"] = row["delivered"] or 0.0
        for row in self._rows(
            "SELECT destination, last_attempt, last_error FROM deliveries d WHERE last_error<>'' AND last_attempt="
            "(SELECT MAX(last_attempt) FROM deliveries WHERE destination=d.destination AND last_error<>'')"):
            result[row["destination"]].update(last_failure=row["last_attempt"], last_error=row["last_error"])
        return result

    def prune(self, days: int) -> int:
        """Delete the finished deliveries (delivered or dead) older than `days`, and the events that no delivery needs."""
        cutoff = time.time() - days * 86400
        with self._transaction() as db:
            count = db.execute("DELETE FROM deliveries WHERE status IN ('delivered', 'dead') AND updated<?", (cutoff,)).rowcount
            db.execute("DELETE FROM events WHERE received<? AND event_id NOT IN (SELECT event_id FROM deliveries)", (cutoff,))
        return count
