"""Copy a hermes-mail installation to uni-mail.

The migration copies and never moves: the hermes-mail config and state stay
as they were, so going back means starting hermes-mail again. It refuses to
run while a hermes-mail daemon answers, and it never overwrites a uni-mail
config or state that exists. Without --apply it only reports the plan.

Copied: the index (with the SQLite backup API, so the copy is consistent),
the refresh tokens, the account changes (settings.json), the stored
passwords and the password files that the config names below the old config
directory. The cache and the exports only with --with-cache; the daemon
fetches them again when needed.

The config keeps every account setting. `notify` and `triage_retention_days`
are dropped and listed in the report: events now go to the "dispatch"
destinations.
"""

from __future__ import annotations

import copy
import grp
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_SOCKET, ConfigError, parse
from .server import legacy_daemon_running

OLD_SOCKET = "/run/hermes-mail/mail.sock"
OLD_UNITS = ("hermes-mail.service", "hermes-mail-notify.service")
STATE_FILES = ("settings.json",)
SECRET_DIRS = ("tokens", "passwords")


def _under(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _active_units() -> list[str]:
    if not shutil.which("systemctl"):
        return []
    active = []
    for unit in OLD_UNITS:
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False)
        if result.returncode == 0:
            active.append(unit)
    return active


def _has_state(directory: Path) -> list[str]:
    """The files of a state directory that a migration would overwrite."""
    found = [name for name in ("index.sqlite3", *STATE_FILES) if (directory / name).exists()]
    found += [name for name in SECRET_DIRS if (directory / name).is_dir() and any((directory / name).iterdir())]
    return found


class Migration:
    def __init__(self, from_config: Path, to_config: Path, state_dir: Path, owner: str = "uni-mail", with_cache: bool = False):
        self.from_config = from_config
        self.to_config = to_config
        self.state_dir = state_dir
        self.owner = owner
        self.with_cache = with_cache
        self.actions: list[str] = []
        self.problems: list[str] = []
        self.removed: dict[str, Any] = {}
        self.copies: list[tuple[Path, Path, int]] = []
        try:
            self.raw = json.loads(from_config.read_text())
        except (OSError, ValueError) as error:
            raise ConfigError(f"cannot read {from_config}: {error}") from None
        self.old_state = Path(self.raw.get("state_dir") or "/var/lib/hermes-mail")
        self.new_raw = self._config()

    def _map(self, value: str) -> str:
        """A path below the old config directory or the old state goes to the same place below the new one."""
        for old, new in ((self.from_config.parent, self.to_config.parent), (self.old_state, self.state_dir)):
            if _under(value, old):
                return str(new / Path(value).resolve().relative_to(old.resolve()))
        return value

    def _config(self) -> dict[str, Any]:
        raw = copy.deepcopy(self.raw)
        raw["state_dir"] = str(self.state_dir)
        if not raw.get("socket") or raw["socket"] == OLD_SOCKET:
            raw["socket"] = DEFAULT_SOCKET
        if "triage_retention_days" in raw:
            self.removed["triage_retention_days"] = raw.pop("triage_retention_days")
        for key in ("export_dir",):
            if raw.get(key):
                raw[key] = self._map(raw[key])
        for name, account in (raw.get("accounts") or {}).items():
            if "notify" in account:
                self.removed.setdefault("notify", {})[name] = account.pop("notify")
            password_file = account.get("password_file")
            if password_file and _under(password_file, self.from_config.parent):
                account["password_file"] = self._map(password_file)
                # Only the daemon reads it: the dispatcher is in the group.
                self.copies.append((Path(password_file), Path(account["password_file"]), 0o600))
        raw.setdefault("event_retention_days", 14)
        return raw

    def check(self) -> None:
        if legacy_daemon_running(Path(self.raw.get("socket") or OLD_SOCKET)):
            self.problems.append("a hermes-mail daemon answers on its socket; run: systemctl disable --now " + " ".join(OLD_UNITS))
        for unit in _active_units():
            self.problems.append(f"{unit} is active; run: systemctl disable --now {unit}")
        if self.to_config.exists():
            self.problems.append(f"{self.to_config} exists; move it away first (the migration never overwrites it)")
        if self.state_dir.resolve() == self.old_state.resolve():
            self.problems.append("the new state directory is the hermes-mail one; the migration makes a copy")
        existing = _has_state(self.state_dir) if self.state_dir.exists() else []
        if existing:
            self.problems.append(f"{self.state_dir} already holds {', '.join(existing)}; the migration never overwrites state")
        if not (self.old_state / "index.sqlite3").exists():
            self.problems.append(f"no index in {self.old_state}; nothing to migrate")
        for source, _, _ in self.copies:
            if not source.is_file():
                self.problems.append(f"the password file {source} is missing")
        try:
            parse(self.new_raw)
        except ConfigError as error:
            self.problems.append(f"the new config is not valid: {error}")
        if os.geteuid() == 0:
            try:
                pwd.getpwnam(self.owner)
            except KeyError:
                self.problems.append(f"the user {self.owner} does not exist; install deploy/sysusers.d/uni-mail.conf first")

    def plan(self) -> list[str]:
        steps = [f"write {self.to_config} (state_dir {self.state_dir}, socket {self.new_raw['socket']})"]
        steps.append(f"copy {self.old_state}/index.sqlite3 with the SQLite backup API, then check it")
        for name in (*SECRET_DIRS, *STATE_FILES):
            if (self.old_state / name).exists():
                steps.append(f"copy {self.old_state / name} (mode 0600 files)")
        if self.with_cache:
            steps += [f"copy {self.old_state / name}" for name in ("cache", "exports") if (self.old_state / name).is_dir()]
        steps += [f"copy {source} to {target}" for source, target, _ in self.copies]
        if os.geteuid() == 0:
            steps.append(f"give the new state to {self.owner}:{self.owner} and the config to root:{self.owner} mode 0640")
        steps.append(f"write {self.state_dir}/MIGRATED-FROM-HERMES-MAIL.json; leave {self.old_state} unchanged")
        return steps

    # Apply

    def _ids(self) -> tuple[int, int] | None:
        if os.geteuid() != 0:
            return None
        user = pwd.getpwnam(self.owner)
        try:
            group = grp.getgrnam(self.owner).gr_gid
        except KeyError:
            group = user.pw_gid
        return user.pw_uid, group

    def _own(self, path: Path, ids: tuple[int, int] | None) -> None:
        if ids is not None:
            os.chown(path, *ids, follow_symlinks=False)

    def _copy_file(self, source: Path, target: Path, mode: int, ids: tuple[int, int] | None) -> None:
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle, source.open("rb") as original:
            shutil.copyfileobj(original, handle)
        os.chmod(target, mode)
        self._own(target, ids)
        self.actions.append(f"copied {source} -> {target}")

    def _copy_index(self, index: Path) -> None:
        """Back up the old index to `index`. SQLite creates its -shm file even in a read-only connection, and
        this may run as root, so the old state directory must not be opened at all: the backup reads a staging
        copy of the database and of its write-ahead log in the new state directory."""
        staging = self.state_dir / "index.migrating"
        names = [f"{staging.name}{suffix}" for suffix in ("", "-wal", "-shm")]
        try:
            for suffix in ("", "-wal"):
                original = self.old_state / f"index.sqlite3{suffix}"
                if original.is_file():
                    shutil.copyfile(original, self.state_dir / f"{staging.name}{suffix}")
            source = sqlite3.connect(staging)
            target = sqlite3.connect(index)
            try:
                source.backup(target)
                check = target.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise ConfigError(f"the copied index failed its integrity check: {check}")
            finally:
                source.close()
                target.close()
        finally:
            for name in names:
                (self.state_dir / name).unlink(missing_ok=True)

    def apply(self) -> None:
        ids = self._ids()
        self.state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o750)
        self._own(self.state_dir, ids)
        index = self.state_dir / "index.sqlite3"
        self._copy_index(index)
        os.chmod(index, 0o640)
        self._own(index, ids)
        self.actions.append(f"copied {self.old_state / 'index.sqlite3'} -> {index} (integrity ok)")
        for name in SECRET_DIRS:
            folder = self.old_state / name
            if folder.is_dir():
                (self.state_dir / name).mkdir(mode=0o700, exist_ok=True)
                self._own(self.state_dir / name, ids)
                for item in sorted(folder.iterdir()):
                    if item.is_file() and not item.name.endswith(".tmp"):
                        self._copy_file(item, self.state_dir / name / item.name, 0o600, ids)
        for name in STATE_FILES:
            if (self.old_state / name).is_file():
                self._copy_file(self.old_state / name, self.state_dir / name, 0o600, ids)
        if self.with_cache:
            for name in ("cache", "exports"):
                folder = self.old_state / name
                if folder.is_dir():
                    for item in sorted(folder.rglob("*")):
                        target_item = self.state_dir / name / item.relative_to(folder)
                        if item.is_dir():
                            target_item.mkdir(mode=0o750, parents=True, exist_ok=True)
                        elif item.is_file():
                            self._copy_file(item, target_item, item.stat().st_mode & 0o777, ids)
                    # The daemon must be able to add and trim entries in every directory.
                    for directory in [self.state_dir / name, *(path for path in (self.state_dir / name).rglob("*") if path.is_dir())]:
                        self._own(directory, ids)
        for source_path, target_path, mode in self.copies:
            target_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            self._own(target_path.parent, (0, ids[1]) if ids else None)
            self._copy_file(source_path, target_path, mode, ids)
        self.to_config.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = os.open(self.to_config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(self.new_raw, handle, indent=2)
            handle.write("\n")
        self._own(self.to_config, (0, ids[1]) if ids else None)
        self.actions.append(f"wrote {self.to_config}")
        marker = self.state_dir / "MIGRATED-FROM-HERMES-MAIL.json"
        marker.write_text(json.dumps({"from_config": str(self.from_config), "from_state": str(self.old_state),
                                      "migrated": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n")
        self._own(marker, ids)

    def report(self, applied: bool) -> dict[str, Any]:
        result: dict[str, Any] = {"applied": applied, "from": {"config": str(self.from_config), "state_dir": str(self.old_state)},
                                  "to": {"config": str(self.to_config), "state_dir": str(self.state_dir)}}
        result["actions" if applied else "plan"] = self.actions if applied else self.plan()
        if self.removed:
            result["removed_settings"] = self.removed
            result["note"] = ("notify and the triage settings are gone: uni-mail makes no decision about mail. "
                              "Add destinations to the dispatch section; see docs/MIGRATION.md.")
        if self.problems:
            result["problems"] = self.problems
        result["next"] = [
            "systemctl daemon-reload",
            "systemctl enable --now uni-mail.service",
            "uni-mail status   # the accounts must reach idle without a new sign-in",
            "systemctl enable --now uni-mail-dispatch.service   # after adding destinations",
        ] if applied else ["run again with --apply"]
        return result


def run(args: Any) -> dict[str, Any]:
    migration = Migration(Path(args.from_config), Path(args.to_config), Path(args.state_dir), args.owner, args.with_cache)
    migration.check()
    if migration.problems or not args.apply:
        result = migration.report(applied=False)
        if migration.problems:
            result["ok"] = False
        return result
    migration.apply()
    return migration.report(applied=True)
