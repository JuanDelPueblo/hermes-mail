"""Dashboard backend for the Mail tab, mounted at /api/plugins/hermes-mail/.

Two owners keep the settings:

- hermes-maild owns the accounts. This module changes them over the service
  socket, and the service keeps them on top of the base accounts.
- The plugin owns the notifications and the triage model. They are plugin
  settings in plugins.entries.hermes-mail.settings, declared in the
  config_schema of plugin.yaml. This module writes them with the plugin
  settings writer of Hermes, and the notifier reads them with ctx.get_config.

Every route answers {"ok": true, ...} or {"ok": false, "error": "..."}.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter()

PLUGIN_ID = "hermes-mail"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SOCKET = "/run/hermes-mail/mail.sock"
ACCOUNT_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
MODES = ("triage", "all", "none")
POLICY_LIMIT = 64 * 1024
TIMEOUT = 60


class MailError(Exception):
    pass


def _settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        entry = ((load_config() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_ID) or {}
        return entry.get("settings") or {}
    except Exception:
        return {}


def _save(values: Dict[str, Any]) -> None:
    """Write plugin settings with the writer of the Hermes plugin settings.
    It checks each value against config_schema."""
    from hermes_cli.plugins_settings import save_plugin_settings

    try:
        save_plugin_settings(PLUGIN_ID, PLUGIN_DIR, values)
    except (ValueError, PermissionError) as error:
        raise MailError(str(error)) from error


def _socket_path() -> str:
    return _settings().get("socket") or os.environ.get("HERMES_MAIL_SOCKET") or DEFAULT_SOCKET


def _call(op: str, **fields: Any) -> Dict[str, Any]:
    """Send one request to hermes-maild: a JSON line out, a JSON line back."""
    path = _socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(TIMEOUT)
        try:
            connection.connect(path)
            connection.sendall(json.dumps({"op": op, **fields}).encode() + b"\n")
            data = connection.makefile("rb").readline()
        except (FileNotFoundError, ConnectionRefusedError):
            raise MailError(f"the mail service is not running (no listener on {path})") from None
        except PermissionError:
            raise MailError(f"no permission to use the mail service socket {path}") from None
        except OSError as error:
            raise MailError(f"the mail service did not answer: {error}") from None
    try:
        response = json.loads(data)
    except ValueError:
        raise MailError("the mail service sent a response that is not JSON") from None
    if not response.get("ok"):
        raise MailError(response.get("error") or "the mail request failed")
    return response


def _account(name: str) -> str:
    if not ACCOUNT_NAME.fullmatch(name):
        raise MailError(f"account name {name!r} must match {ACCOUNT_NAME.pattern}")
    return name


def _answer(action) -> Dict[str, Any]:
    try:
        return {"ok": True, **(action() or {})}
    except MailError as error:
        return {"ok": False, "error": str(error)}


def _notify(values: Any) -> Dict[str, Any]:
    """Check one notification entry from the page and return it in the form
    that the notifier reads."""
    if not isinstance(values, dict):
        raise MailError("the notification settings must be a JSON object")
    mode = values.get("mode", "none")
    if mode not in MODES:
        raise MailError(f"mode must be one of {', '.join(MODES)}")
    target = str(values.get("target") or "").strip()
    if mode != "none" and not target:
        raise MailError(f"a target is required when the mode is {mode}")
    policy = str(values.get("policy") or "")
    if len(policy.encode()) > POLICY_LIMIT:
        raise MailError("the policy is too long")
    try:
        command = shlex.split(str(values.get("task_command") or ""))
    except ValueError as error:
        raise MailError(f"cannot read the task command: {error}") from None
    return {
        "mode": mode,
        "target": target,
        "policy": policy,
        "mark_read_silent": bool(values.get("mark_read_silent")),
        "task_command": command,
    }


def _base_notify(notify: Dict[str, Any]) -> Dict[str, Any]:
    """The base notifications of an account, with the policy file as text."""
    policy, policy_error = "", ""
    if notify.get("policy_file"):
        try:
            policy = Path(notify["policy_file"]).read_text()
        except OSError as error:
            policy_error = f"cannot read the policy file: {error}"
    return {
        "mode": notify.get("mode") or "none",
        "target": notify.get("target") or "",
        "policy": notify.get("policy") or policy,
        "policy_error": policy_error,
        "mark_read_silent": bool(notify.get("mark_read_silent")),
        "task_command": list(notify.get("task_command") or []),
    }


@router.get("/settings")
def settings() -> Dict[str, Any]:
    def read() -> Dict[str, Any]:
        service = _call("settings")
        running = _call("accounts")["accounts"]
        status = {item["name"]: item for item in _call("status")["accounts"]}
        plugin = _settings()
        overrides = plugin.get("notify") if isinstance(plugin.get("notify"), dict) else {}
        accounts = []
        for account in service["accounts"]:
            name = account["name"]
            state = status.get(name) or {}
            base = _base_notify((running.get(name) or {}).get("notify") or {})
            changed = overrides.get(name)
            accounts.append({
                **account,
                "status": state.get("status", "removed" if account["removed"] else "not running"),
                "status_error": state.get("error", ""),
                "last_sync": state.get("last_sync", ""),
                "notify": {"base": base, "dashboard": changed, "current": changed or base},
            })
        return {
            "socket": _socket_path(),
            "editable": service["editable"],
            "providers": service["providers"],
            "default_hosts": service["default_hosts"],
            "default_archive_folders": service["default_archive_folders"],
            "accounts": accounts,
            "triage": {"provider": plugin.get("triage_provider") or "", "model": plugin.get("triage_model") or ""},
        }

    return _answer(read)


@router.get("/accounts/{name}/folders")
def folders(name: str) -> Dict[str, Any]:
    def read() -> Dict[str, Any]:
        response = _call("folders", account=_account(name))
        return {key: response[key] for key in ("folders", "archive_folder", "synced")}

    return _answer(read)


@router.post("/accounts/{name}")
def save_account(name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    def save() -> None:
        fields: Dict[str, Any] = {"account": _account(name), "settings": body.get("settings") or {}}
        if body.get("password"):
            fields["password"] = body["password"]
        _call("settings_save", **fields)

    return _answer(save)


@router.post("/accounts/{name}/remove")
def remove_account(name: str) -> Dict[str, Any]:
    def remove() -> None:
        _call("settings_delete", account=_account(name))
        overrides = dict(_settings().get("notify") or {})
        if overrides.pop(name, None) is not None:
            _save({"notify": overrides})

    return _answer(remove)


@router.post("/accounts/{name}/reset")
def reset_account(name: str) -> Dict[str, Any]:
    def reset() -> None:
        _call("settings_reset", account=_account(name))

    return _answer(reset)


@router.post("/accounts/{name}/notify")
def save_notify(name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    def save() -> None:
        _account(name)
        overrides = dict(_settings().get("notify") or {})
        overrides[name] = _notify(body)
        _save({"notify": overrides})

    return _answer(save)


@router.post("/accounts/{name}/notify/reset")
def reset_notify(name: str) -> Dict[str, Any]:
    def reset() -> None:
        _account(name)
        overrides = dict(_settings().get("notify") or {})
        if overrides.pop(name, None) is not None:
            _save({"notify": overrides})

    return _answer(reset)


STATUSES = ("notified", "silent", "error", "dispatched", "no_report")


@router.get("/activity")
def activity(account: str = "", status: str = "", query: str = "", since: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """The triage log, newest first."""
    def read() -> Dict[str, Any]:
        if account:
            _account(account)
        if status and status not in STATUSES:
            raise MailError(f"status must be one of {', '.join(STATUSES)}")
        fields = {"account": account, "status": status, "query": query, "since": since,
                  "limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        response = _call("triage_list", **{key: value for key, value in fields.items() if value not in ("", None)})
        return {key: response[key] for key in ("entries", "count", "retention_days")}

    return _answer(read)


@router.get("/activity/{entry_id}")
def activity_entry(entry_id: int) -> Dict[str, Any]:
    def read() -> Dict[str, Any]:
        response = _call("triage_show", id=entry_id)
        return {"entry": {key: value for key, value in response.items() if key != "ok"}}

    return _answer(read)


@router.post("/activity/{entry_id}/retry")
def retry_activity(entry_id: int) -> Dict[str, Any]:
    """Queue the mail of a log entry for the notifier again."""
    def retry() -> Dict[str, Any]:
        response = _call("triage_retry", id=entry_id)
        return {"mail_id": response["mail_id"]}

    return _answer(retry)


@router.post("/triage")
def save_triage(body: Dict[str, Any]) -> Dict[str, Any]:
    def save() -> None:
        _save({
            "triage_provider": str(body.get("provider") or "").strip(),
            "triage_model": str(body.get("model") or "").strip(),
        })

    return _answer(save)


@router.post("/accounts/{name}/signin")
def begin_sign_in(name: str) -> Dict[str, Any]:
    def begin() -> Dict[str, Any]:
        response = _call("auth_begin", account=_account(name))
        return {"url": response["url"], "redirect_uri": response["redirect_uri"]}

    return _answer(begin)


@router.post("/accounts/{name}/signin/finish")
def finish_sign_in(name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    def finish() -> Dict[str, Any]:
        response = _call("auth_finish", account=_account(name), redirect=str(body.get("redirect") or ""))
        return {"message": response["message"]}

    return _answer(finish)
