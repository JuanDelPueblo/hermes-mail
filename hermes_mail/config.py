"""Service configuration, read from a JSON file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = "/run/hermes-mail/mail.sock"
PROVIDERS = ("microsoft", "google", "imap")
NOTIFY_MODES = ("triage", "all", "none")
ACCOUNT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# mail_archive moves a message here when the account sets no archive_folder.
# Gmail treats "[Gmail]/All Mail" as the archive: moving a message there just
# drops its Inbox label, since every message already lives in All Mail. The
# `imap` provider has no default; archiving needs an explicit archive_folder.
DEFAULT_ARCHIVE_FOLDER = {"microsoft": "Archive", "google": "[Gmail]/All Mail"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Notify:
    mode: str = "none"
    target: str = ""
    policy_file: str = ""
    policy: str = ""
    mark_read_silent: bool = False
    task_command: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "target": self.target,
            "policy_file": self.policy_file,
            "policy": self.policy,
            "mark_read_silent": self.mark_read_silent,
            "task_command": list(self.task_command),
        }


@dataclass(frozen=True)
class Account:
    name: str
    provider: str
    address: str
    auth: str = "oauth"
    host: str = ""
    port: int = 993
    password_file: str = ""
    folders: tuple[str, ...] = ("INBOX",)
    archive_folder: str = ""
    sync_days: int = 7
    poll_seconds: int = 300
    cache_limit_mb: int = 100
    max_part_mb: int = 25
    notify: Notify = field(default_factory=Notify)


@dataclass(frozen=True)
class Config:
    state_dir: Path
    socket: Path
    export_dir: Path
    extract_root: Path | None
    accounts: dict[str, Account]
    # The accounts as the config file holds them, before the web settings.
    base: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Accept account changes from the socket (the Hermes dashboard).
    web_settings: bool = True
    # Days to keep the triage log. 0 keeps it for ever.
    triage_retention_days: int = 30


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def parse_account(name: str, raw: dict[str, Any]) -> Account:
    _require(bool(ACCOUNT_NAME.fullmatch(name)), f"account name {name!r} must match {ACCOUNT_NAME.pattern}")
    provider = raw.get("provider", "")
    _require(provider in PROVIDERS, f"account {name}: provider must be one of {', '.join(PROVIDERS)}")
    auth = raw.get("auth") or ("password" if provider == "imap" else "oauth")
    _require(auth in ("oauth", "password"), f"account {name}: auth must be oauth or password")
    _require(not (provider == "imap" and auth == "oauth"), f"account {name}: the imap provider supports only password sign-in")
    _require(bool(raw.get("address")), f"account {name}: address is required")
    if auth == "password":
        _require(bool(raw.get("password_file")), f"account {name}: password sign-in needs password_file")
    if provider == "imap":
        _require(bool(raw.get("host")), f"account {name}: the imap provider needs host")
    folders = tuple(raw.get("folders") or ("INBOX",))
    notify_raw = raw.get("notify") or {}
    mode = notify_raw.get("mode", "none")
    _require(mode in NOTIFY_MODES, f"account {name}: notify.mode must be one of {', '.join(NOTIFY_MODES)}")
    if mode != "none":
        _require(bool(notify_raw.get("target")), f"account {name}: notify.target is required when notify.mode is {mode}")
    sync_days = int(raw.get("sync_days", 7))
    _require(1 <= sync_days <= 365, f"account {name}: sync_days must be 1 to 365")
    return Account(
        name=name,
        provider=provider,
        address=raw["address"],
        auth=auth,
        host=raw.get("host") or "",
        port=int(raw.get("port") or 993),
        password_file=raw.get("password_file") or "",
        folders=folders,
        archive_folder=raw.get("archive_folder") or DEFAULT_ARCHIVE_FOLDER.get(provider, ""),
        sync_days=sync_days,
        poll_seconds=max(30, int(raw.get("poll_seconds", 300))),
        cache_limit_mb=max(1, int(raw.get("cache_limit_mb", 100))),
        max_part_mb=max(1, int(raw.get("max_part_mb", 25))),
        notify=Notify(
            mode=mode,
            target=notify_raw.get("target") or "",
            policy_file=notify_raw.get("policy_file") or "",
            policy=notify_raw.get("policy") or "",
            mark_read_silent=bool(notify_raw.get("mark_read_silent", False)),
            task_command=tuple(notify_raw.get("task_command") or ()),
        ),
    )


def parse(raw: dict[str, Any]) -> Config:
    _require(bool(raw.get("state_dir")), "state_dir is required")
    base = {name: dict(value) for name, value in (raw.get("accounts") or {}).items()}
    accounts = {name: parse_account(name, value) for name, value in base.items()}
    state_dir = Path(raw["state_dir"])
    retention = int(raw.get("triage_retention_days", 30))
    _require(0 <= retention <= 3650, "triage_retention_days must be 0 to 3650")
    return Config(
        state_dir=state_dir,
        socket=Path(raw.get("socket") or DEFAULT_SOCKET),
        export_dir=Path(raw.get("export_dir") or state_dir / "exports"),
        extract_root=Path(raw["extract_root"]) if raw.get("extract_root") else None,
        accounts=accounts,
        base=base,
        web_settings=bool(raw.get("web_settings", True)),
        triage_retention_days=retention,
    )


def load(path: str | Path) -> Config:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    return parse(raw)
