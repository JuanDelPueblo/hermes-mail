"""The uni-mail event format, version 1.

An event says that something happened to an account: new mail, a refused
sign-in, a long sync failure. It carries metadata only. It never carries the
text of a message, an attachment or a credential; a consumer reads the mail
through the socket, the CLI or the MCP server.

    {
      "schema_version": 1,
      "event_id": "6f1c0a4e-...",          stable for the life of the event
      "event_type": "mail.new",
      "seq": 42,                            order in the daemon's event log
      "account": "university",
      "mail_id": "university.3f2a9c1d0b7e4a55",   "" when not about one mail
      "timestamp": "2026-10-15T12:00:00+00:00",
      "data": {...}
    }

`data` of `mail.new`: folder, message_id, date, sender, recipients, cc,
subject, read, flagged, size and attachment_count. `data` of `mail.auth` and
`mail.error`: detail, the error text for the operator.

This module uses only the standard library, so that independent consumers can
copy it.
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
EVENT_TYPES = ("mail.new", "mail.auth", "mail.error")
MAIL_FIELDS = ("folder", "message_id", "date", "sender", "recipients", "cc", "subject", "read", "flagged", "size")
# Header values come from the sender, so keep them short.
FIELD_LIMIT = 1000


def new_event_id() -> str:
    return str(uuid.uuid4())


def mail_data(record: dict[str, Any], attachment_count: int) -> dict[str, Any]:
    """The `data` of a mail.new event: the index metadata, never the text."""
    data: dict[str, Any] = {}
    for key in MAIL_FIELDS:
        value = record.get(key)
        if isinstance(value, str):
            value = value[:FIELD_LIMIT]
        data[key] = value
    data["attachment_count"] = attachment_count
    return data


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def build(*, event_id: str, event_type: str, seq: int, account: str, mail_id: str, created: float,
          data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "seq": seq,
        "account": account,
        "mail_id": mail_id,
        "timestamp": iso(created),
        "data": data,
    }


def validate(event: Any) -> dict[str, Any]:
    """Check the shape of an event. Raise ValueError with the reason."""
    if not isinstance(event, dict):
        raise ValueError("the event is not a JSON object")
    if event.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {event.get('schema_version')!r}")
    for key in ("event_id", "event_type", "account", "timestamp"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise ValueError(f"the event has no {key}")
    if not isinstance(event.get("mail_id", ""), str):
        raise ValueError("mail_id must be text")
    if event["event_type"] == "mail.new" and not event.get("mail_id"):
        raise ValueError("a mail.new event needs a mail_id")
    if not isinstance(event.get("data", {}), dict):
        raise ValueError("data must be an object")
    return event


def matches(patterns: tuple[str, ...] | list[str], value: str) -> bool:
    """Whether `value` matches one of the shell-style patterns, such as mail.*."""
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def dumps(event: dict[str, Any]) -> bytes:
    """The canonical bytes of an event: what a webhook sends and a command reads."""
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
