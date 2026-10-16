#!/usr/bin/env python3
"""Bridge a uni-mail event to a Nanobot trigger.

This is a uni-mail *command sink*: the dispatcher runs it, writes one event
(JSON, schema v1) to its stdin and reads the exit status.

    uni_mail_nanobot_trigger.py --trigger-id trg_XXXXXXXX [--nanobot PATH]
                                [--workspace DIR] [--config FILE]
                                [--events mail.new] [--timeout 60]

For an event it wants, it runs `nanobot trigger <id> <message>`. The message
holds only identifiers (account, mail ID, event ID), never mail content.
Nanobot then reads the mail through the uni-mail MCP server and decides what
to do with it by its own instructions. This script does not read, classify or
judge any mail.

Exit status (matches uni-mail's default permanent_exit_codes [64, 65]):
    0   delivered, or the event type is not wanted (ignored)
    64  bad usage (missing or invalid trigger ID, bad option)
    65  malformed event (retrying cannot help)
    75  nanobot is missing, failed or timed out (EX_TEMPFAIL, retried)

Delivery is at-least-once, so the same event ID can arrive twice. The event ID
is in the message so that the agent can recognise a repeat.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, BinaryIO, NoReturn

EX_OK = 0
EX_USAGE = 64
EX_DATAERR = 65
EX_TEMPFAIL = 75

SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 1024 * 1024
DETAIL_LIMIT = 300
STDERR_TAIL = 800

ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAIL_ID_RE = re.compile(r"^([a-z][a-z0-9_-]{0,31})\.[0-9a-f]{16}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}$")
TRIGGER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")


class EventError(ValueError):
    """The event is malformed. Retrying the same event cannot help."""


def _text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise EventError(f"the event has no {key}")
    return value


def validate_event(event: Any) -> dict[str, Any]:
    """Check the shape of a uni-mail event (schema v1). Raise EventError."""
    if not isinstance(event, dict):
        raise EventError("the event is not a JSON object")
    version = event.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise EventError(f"unsupported schema_version {version!r}")
    event_id = _text(event.get("event_id"), "event_id")
    if not EVENT_ID_RE.match(event_id):
        raise EventError("event_id has an unexpected form")
    event_type = _text(event.get("event_type"), "event_type")
    if not EVENT_TYPE_RE.match(event_type):
        raise EventError("event_type has an unexpected form")
    account = _text(event.get("account"), "account")
    if not ACCOUNT_RE.match(account):
        raise EventError("account has an unexpected form")
    _text(event.get("timestamp"), "timestamp")
    seq = event.get("seq")
    if seq is not None and (type(seq) is not int or seq < 0):
        raise EventError("seq must be a non-negative integer")
    mail_id = event.get("mail_id", "")
    if not isinstance(mail_id, str):
        raise EventError("mail_id must be text")
    if mail_id:
        match = MAIL_ID_RE.match(mail_id)
        if not match or match.group(1) != account:
            raise EventError("mail_id does not belong to the account")
    elif event_type == "mail.new":
        raise EventError("a mail.new event needs a mail_id")
    data = event.get("data", {})
    if not isinstance(data, dict):
        raise EventError("data must be an object")
    return event


def is_synthetic(event: dict[str, Any]) -> bool:
    data = event.get("data")
    return isinstance(data, dict) and data.get("synthetic") is True


def one_line(value: Any, limit: int = DETAIL_LIMIT) -> str:
    """Collapse text to a single short line of printable characters."""
    text = _CONTROL_RE.sub(" ", str(value if value is not None else ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def build_message(event: dict[str, Any]) -> str:
    """The text handed to nanobot. Identifiers only, never mail content."""
    event_type = event["event_type"]
    account = event["account"]
    event_id = event["event_id"]
    if event_type == "mail.new":
        message = (
            f"New email received in account {account}, mail ID {event['mail_id']}, "
            f"event ID {event_id} ({event_type}). Retrieve the message through the "
            "uni-mail MCP server and process it according to your existing instructions."
        )
    elif event_type in ("mail.auth", "mail.error"):
        data = event.get("data") or {}
        detail = one_line(data.get("detail", "")) or "no detail"
        message = (
            f"uni-mail reported {event_type} for account {account} (event ID {event_id}): "
            f"{detail}. No message to retrieve."
        )
    else:
        message = f"uni-mail reported {event_type} for account {account} (event ID {event_id})."
        if event.get("mail_id"):
            message += f" Mail ID {event['mail_id']}."
    if is_synthetic(event):
        message = "[test] " + message
    return message


def build_argv(nanobot: str, trigger_id: str, message: str, workspace: str | None = None,
               config: str | None = None) -> list[str]:
    argv = [nanobot, "trigger", trigger_id]
    if workspace:
        argv += ["-w", workspace]
    if config:
        argv += ["-c", config]
    argv.append(message)
    return argv


def run_nanobot(argv: list[str], timeout: float) -> tuple[bool, str]:
    """Run nanobot without a shell. Return (ok, detail); detail is the stderr tail on failure."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return False, f"nanobot not found: {argv[0]}"
    except PermissionError:
        return False, f"nanobot is not executable: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, f"nanobot did not finish within {timeout:g} s"
    except OSError as error:
        return False, f"could not run nanobot: {error}"
    if done.returncode == 0:
        return True, one_line(done.stdout.decode("utf-8", "replace"), STDERR_TAIL)
    # nanobot prints its errors on stdout or stderr, depending on the message.
    output = done.stderr.decode("utf-8", "replace").strip() or done.stdout.decode("utf-8", "replace").strip()
    tail = one_line(output[-STDERR_TAIL:], STDERR_TAIL)
    return False, f"nanobot exited with status {done.returncode}" + (f": {tail}" if tail else "")


def parse_events(value: str) -> frozenset[str]:
    names = frozenset(part.strip() for part in value.split(",") if part.strip())
    if not names:
        raise ValueError("--events needs at least one event type")
    for name in names:
        if not EVENT_TYPE_RE.match(name):
            raise ValueError(f"bad event type {name!r}")
    return names


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EX_USAGE)


def make_parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Hand a uni-mail event to a Nanobot trigger (reads the event from stdin).")
    parser.add_argument("--trigger-id", default=os.environ.get("UNI_MAIL_NANOBOT_TRIGGER_ID"),
                        help="trigger ID from /trigger (env UNI_MAIL_NANOBOT_TRIGGER_ID)")
    parser.add_argument("--nanobot", default=os.environ.get("NANOBOT_BIN") or "nanobot",
                        help="the nanobot executable (env NANOBOT_BIN, default: nanobot from PATH)")
    parser.add_argument("--workspace", help="nanobot workspace; must match the gateway's")
    parser.add_argument("--config", help="nanobot config file; must match the gateway's")
    parser.add_argument("--events", default="mail.new", help="comma-separated event types to deliver (default mail.new)")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for nanobot (default 60)")
    return parser


def read_event(stream: BinaryIO) -> dict[str, Any]:
    raw = stream.read(MAX_EVENT_BYTES + 1)
    if len(raw) > MAX_EVENT_BYTES:
        raise EventError(f"the event is larger than {MAX_EVENT_BYTES} bytes")
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise EventError(f"the event is not valid JSON: {error}") from error
    return validate_event(event)


def main(argv: list[str] | None = None, stdin: BinaryIO | None = None) -> int:
    args = make_parser().parse_args(argv)
    trigger_id = (args.trigger_id or "").strip()
    if not trigger_id:
        print("error: a trigger ID is required (--trigger-id or UNI_MAIL_NANOBOT_TRIGGER_ID)", file=sys.stderr)
        return EX_USAGE
    if not TRIGGER_ID_RE.match(trigger_id):
        print("error: the trigger ID has an unexpected form", file=sys.stderr)
        return EX_USAGE
    try:
        wanted = parse_events(args.events)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return EX_USAGE
    if not args.timeout > 0:
        print("error: --timeout must be positive", file=sys.stderr)
        return EX_USAGE

    try:
        event = read_event(stdin if stdin is not None else sys.stdin.buffer)
    except EventError as error:
        print(f"malformed event: {error}", file=sys.stderr)
        return EX_DATAERR
    if event["event_type"] not in wanted:
        print(f"ignored {event['event_type']} event {event['event_id']}: not in --events", file=sys.stderr)
        return EX_OK

    command = build_argv(args.nanobot, trigger_id, build_message(event), args.workspace, args.config)
    ok, detail = run_nanobot(command, args.timeout)
    if not ok:
        print(f"delivery failed: {detail}", file=sys.stderr)
        return EX_TEMPFAIL
    print(f"triggered {trigger_id} for event {event['event_id']}", file=sys.stderr)
    return EX_OK


if __name__ == "__main__":
    sys.exit(main())
