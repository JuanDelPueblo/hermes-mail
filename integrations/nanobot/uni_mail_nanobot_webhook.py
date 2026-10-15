#!/usr/bin/env python3
"""Receive uni-mail webhooks and hand each event to a Nanobot trigger.

Use this when the uni-mail dispatcher cannot run `nanobot trigger` itself, for
instance because it runs on another host or as another user. If both run on
the same machine, the command sink (uni_mail_nanobot_trigger.py) is simpler.

    UNI_MAIL_WEBHOOK_SECRET=... uni_mail_nanobot_webhook.py --trigger-id trg_XXXXXXXX
    uni_mail_nanobot_webhook.py --secret-file /etc/uni-mail/secrets/nanobot-webhook --trigger-id trg_XXXXXXXX

It listens on 127.0.0.1:8790 at /uni-mail by default. Put a TLS reverse proxy
in front of it for anything that is not loopback.

Request checks, in this order: path (404), method (405), size (413), a
signature that matches and a timestamp within 300 s (401), then a well-formed
event (400). The signature is `X-Uni-Mail-Signature: v1=<hex>` with

    hex = HMAC-SHA256(secret.strip(), timestamp + "." + delivery_id + "." + body)

The header may hold several comma-separated v1 values (secret rotation); one
match is enough. Comparison is in constant time.

Responses: 200 delivered, duplicate or ignored; 409 the same event is being
handled right now (the sender retries later); 503 nanobot failed (the sender
retries). Senders retry 401 and 403 as well, so a rotated secret recovers.

Idempotency: the event ID is recorded in SQLite (--state). `running` is set
before nanobot is called, `done` after it succeeds. A failure removes the row,
so the retry runs again. If this process dies after nanobot succeeded but
before `done` is written, the row stays `running` and, once it is older than
--stale-seconds, the retry triggers again: one duplicate trigger is possible
(at-least-once). The message carries the event ID so the agent can spot it.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uni_mail_nanobot_trigger as trigger  # noqa: E402

MAX_BODY = 256 * 1024
TOLERANCE = 300
STALE_SECONDS = 600
HEADER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SIGNATURE_RE = re.compile(r"^v1=([0-9a-fA-F]{64})$")


def sign(secret: str, timestamp: str, delivery_id: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + delivery_id.encode() + b"." + body
    return hmac.new(secret.strip().encode("utf-8"), message, hashlib.sha256).hexdigest()


def signature_ok(secret: str, header: str, timestamp: str, delivery_id: str, body: bytes) -> bool:
    expected = sign(secret, timestamp, delivery_id, body)
    matched = False
    for part in header.split(","):
        found = SIGNATURE_RE.match(part.strip())
        if found and hmac.compare_digest(found.group(1).lower(), expected):
            matched = True  # keep looping: do not leak which value matched
    return matched


class Dedupe:
    """Event IDs already handled, in SQLite. A connection per call keeps threads simple."""

    def __init__(self, path: str, stale_seconds: float = STALE_SECONDS):
        self.path = path
        self.stale_seconds = stale_seconds
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events ("
                       "event_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated REAL NOT NULL)")

    def _connect(self) -> "_Connection":
        return _Connection(self.path)

    def claim(self, event_id: str) -> str:
        """Return `new` (go ahead), `done` (duplicate) or `running` (somebody else is on it)."""
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status, updated FROM events WHERE event_id = ?", (event_id,)).fetchone()
                if row is None:
                    db.execute("INSERT INTO events VALUES (?, 'running', ?)", (event_id, now))
                    result = "new"
                elif row[0] == "done":
                    result = "done"
                elif now - row[1] > self.stale_seconds:
                    db.execute("UPDATE events SET updated = ? WHERE event_id = ?", (now, event_id))
                    result = "new"
                else:
                    result = "running"
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return result

    def finish(self, event_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE events SET status = 'done', updated = ? WHERE event_id = ?", (time.time(), event_id))

    def release(self, event_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM events WHERE event_id = ? AND status = 'running'", (event_id,))


class _Connection:
    """A sqlite3 connection in autocommit mode that is closed on exit."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)

    def __enter__(self) -> sqlite3.Connection:
        return self.db

    def __exit__(self, *exc: object) -> None:
        self.db.close()


class Receiver(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, secret: str, dedupe: Dedupe, nanobot: str, trigger_id: str,
                 workspace: str | None = None, config: str | None = None, events: frozenset[str] | None = None,
                 timeout: float = 10.0, path: str = "/uni-mail", tolerance: int = TOLERANCE,
                 max_body: int = MAX_BODY):
        self.secret = secret
        self.dedupe = dedupe
        self.nanobot = nanobot
        self.trigger_id = trigger_id
        self.workspace = workspace
        self.config = config
        self.events = events or frozenset({"mail.new"})
        self.timeout = timeout
        self.path = path
        self.tolerance = tolerance
        self.max_body = max_body
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: Receiver
    server_version = "uni-mail-nanobot"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        log(f"{self.address_string()} {format % args}")

    def reply(self, status: int, payload: dict[str, str], close: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.method_not_allowed()

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_GET

    def method_not_allowed(self) -> None:
        if self.path.split("?", 1)[0] != self.server.path:
            self.reply(404, {"error": "not found"}, close=True)
        else:
            self.reply(405, {"error": "use POST"}, close=True)

    def do_POST(self) -> None:
        server = self.server
        if self.path.split("?", 1)[0] != server.path:
            return self.reply(404, {"error": "not found"}, close=True)
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 0:
                raise ValueError
        except ValueError:
            return self.reply(400, {"error": "a Content-Length is required"}, close=True)
        if length > server.max_body:
            self.drain(length)
            return self.reply(413, {"error": "the body is too large"}, close=True)
        body = self.rfile.read(length)
        if len(body) != length:
            return self.reply(400, {"error": "the body is incomplete"}, close=True)

        timestamp = (self.headers.get("X-Uni-Mail-Timestamp") or "").strip()
        delivery_id = (self.headers.get("X-Uni-Mail-Delivery-Id") or "").strip()
        header = self.headers.get("X-Uni-Mail-Signature") or ""
        if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12 and HEADER_ID_RE.match(delivery_id)
                and abs(time.time() - int(timestamp)) <= server.tolerance
                and signature_ok(server.secret, header, timestamp, delivery_id, body)):
            log(f"rejected delivery {delivery_id[:40]!r}: bad signature or timestamp")
            return self.reply(401, {"error": "bad signature or timestamp"})

        try:
            event = trigger.validate_event(json.loads(body.decode("utf-8")))
        except (trigger.EventError, UnicodeDecodeError, ValueError) as error:
            log(f"rejected delivery {delivery_id}: malformed event: {error}")
            return self.reply(400, {"error": f"malformed event: {error}"})

        event_id = event["event_id"]
        if event["event_type"] not in server.events:
            log(f"ignored {event['event_type']} event {event_id}")
            return self.reply(200, {"status": "ignored"})
        claim = server.dedupe.claim(event_id)
        if claim == "done":
            log(f"duplicate event {event_id}")
            return self.reply(200, {"status": "duplicate"})
        if claim == "running":
            log(f"event {event_id} is already being handled")
            return self.reply(409, {"status": "busy"})

        command = trigger.build_argv(server.nanobot, server.trigger_id, trigger.build_message(event),
                                     server.workspace, server.config)
        ok, detail = trigger.run_nanobot(command, server.timeout)
        if not ok:
            server.dedupe.release(event_id)
            log(f"event {event_id}: delivery failed: {detail}")
            return self.reply(503, {"status": "failed"})
        try:
            server.dedupe.finish(event_id)
        except sqlite3.Error as error:
            # The trigger is queued. Answer 200 so that the sender does not trigger again.
            log(f"event {event_id}: triggered, but the state could not be written: {error}")
        log(f"event {event_id}: triggered {server.trigger_id}")
        self.reply(200, {"status": "ok"})

    def drain(self, length: int) -> None:
        """Read (and drop) an oversize body so that the client sees the 413, not a reset."""
        remaining = min(length, 4 * self.server.max_body)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", file=sys.stderr, flush=True)


def read_secret(path: str | None) -> str:
    secret = Path(path).read_text(encoding="utf-8") if path else os.environ.get("UNI_MAIL_WEBHOOK_SECRET", "")
    return secret.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Receive uni-mail webhooks and trigger Nanobot.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--path", default="/uni-mail")
    parser.add_argument("--secret-file", help="file with the shared secret (default: env UNI_MAIL_WEBHOOK_SECRET)")
    parser.add_argument("--state", default="./uni-mail-nanobot-webhook.sqlite3", help="SQLite file for event IDs")
    parser.add_argument("--stale-seconds", type=float, default=STALE_SECONDS,
                        help="a 'running' event older than this may be handled again (default 600)")
    parser.add_argument("--trigger-id", default=os.environ.get("UNI_MAIL_NANOBOT_TRIGGER_ID"))
    parser.add_argument("--nanobot", default=os.environ.get("NANOBOT_BIN") or "nanobot")
    parser.add_argument("--workspace")
    parser.add_argument("--config")
    parser.add_argument("--events", default="mail.new")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for nanobot (default 10; keep it below the sender timeout, 15 s by default)")
    args = parser.parse_args(argv)

    try:
        secret = read_secret(args.secret_file)
    except OSError as error:
        print(f"error: cannot read the secret: {error}", file=sys.stderr)
        return trigger.EX_USAGE
    if not secret:
        print("error: no secret; use --secret-file or UNI_MAIL_WEBHOOK_SECRET", file=sys.stderr)
        return trigger.EX_USAGE
    trigger_id = (args.trigger_id or "").strip()
    if not trigger.TRIGGER_ID_RE.match(trigger_id):
        print("error: a valid --trigger-id (or UNI_MAIL_NANOBOT_TRIGGER_ID) is required", file=sys.stderr)
        return trigger.EX_USAGE
    try:
        events = trigger.parse_events(args.events)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return trigger.EX_USAGE
    if not args.path.startswith("/"):
        print("error: --path must start with /", file=sys.stderr)
        return trigger.EX_USAGE

    server = Receiver((args.host, args.port), secret=secret, dedupe=Dedupe(args.state, args.stale_seconds),
                      nanobot=args.nanobot, trigger_id=trigger_id, workspace=args.workspace, config=args.config,
                      events=events, timeout=args.timeout, path=args.path)
    log(f"listening on {args.host}:{server.server_address[1]}{args.path}, trigger {trigger_id}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
