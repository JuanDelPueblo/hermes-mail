"""hermes-maild: the service. It runs one worker per account and a Unix socket.

Protocol: the client sends one JSON object and a newline. The service sends
one JSON object and a newline, then closes the connection. Every response has
"ok". A failed request has "error" with the text for the operator.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socketserver
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import auth, config, mime
from .account import MailAccount, MailError, attachment_part, attachments, default_imap_factory
from .store import Store

log = logging.getLogger("hermes_mail")

REQUEST_LIMIT = 1024 * 1024
MAX_WAIT = 300


class RequestError(Exception):
    pass


def parse_time(value: str) -> str:
    """Return a UTC ISO time for 2d, 12h, 30m, 1w, yesterday or an ISO date."""
    now = datetime.now(timezone.utc)
    text = value.strip().lower()
    if text == "yesterday":
        local = datetime.now().astimezone()
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return midnight.astimezone(timezone.utc).isoformat()
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if text[:-1].isdigit() and text[-1:] in units:
        return (now - timedelta(**{units[text[-1]]: int(text[:-1])})).isoformat()
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise RequestError(f"cannot read the time {value!r}; use 2d, 12h, yesterday or an ISO date") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc).isoformat()


class Service:
    def __init__(self, cfg: config.Config, imap_factory: Callable = default_imap_factory):
        self.cfg = cfg
        self.store = Store(cfg.state_dir)
        self.tokens = auth.TokenStore(cfg.state_dir / "tokens")
        self.logins = auth.LoginFlow(self.tokens)
        self.accounts = {name: MailAccount(account, self.store, self.tokens, imap_factory) for name, account in cfg.accounts.items()}
        self.store.drop_other_accounts(self.accounts)
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for account in self.accounts.values():
            thread = threading.Thread(target=account.run, name=f"account-{account.name}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        for account in self.accounts.values():
            account.close()

    # Helpers

    def _account(self, name: str) -> MailAccount:
        try:
            return self.accounts[name]
        except KeyError:
            raise RequestError(f"unknown account {name!r}; accounts: {', '.join(sorted(self.accounts))}") from None

    def _record(self, identifier: Any) -> dict[str, Any]:
        record = self.store.get(str(identifier))
        if record is None:
            raise RequestError(f"message not found: {identifier} (it may be older than the sync window)")
        return record

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        return {key: record[key] for key in (
            "id", "account", "folder", "message_id", "date", "sender", "recipients", "cc", "subject", "read", "flagged", "size",
        )}

    # Operations

    def op_status(self, _: dict[str, Any]) -> dict[str, Any]:
        accounts = []
        for account in self.accounts.values():
            synced = self.store.last_sync(account.name)
            accounts.append({
                "name": account.name,
                "provider": account.cfg.provider,
                "address": account.cfg.address,
                "status": account.status,
                "error": account.error,
                "last_sync": datetime.fromtimestamp(synced, timezone.utc).isoformat() if synced else "",
                "sync_days": account.cfg.sync_days,
                "folders": list(account.cfg.folders),
                **self.store.counts(account.name),
            })
        return {"accounts": accounts}

    def op_accounts(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"accounts": {name: {"address": account.cfg.address, "notify": account.cfg.notify.to_dict()}
                             for name, account in self.accounts.items()}}

    def op_list(self, request: dict[str, Any]) -> dict[str, Any]:
        account = request.get("account") or ""
        if account:
            self._account(account)
        messages = self.store.query(
            account=account,
            since=parse_time(request["since"]) if request.get("since") else "",
            until=parse_time(request["until"]) if request.get("until") else "",
            unread=bool(request.get("unread")),
            text=str(request.get("query") or ""),
            limit=int(request.get("limit") or 50),
        )
        return {"count": len(messages), "messages": messages}

    def op_show(self, request: dict[str, Any]) -> dict[str, Any]:
        record = self._record(request.get("id"))
        account = self._account(record["account"])
        result = {**self._summary(record), "attachments": attachments(record)}
        if request.get("full", True):
            try:
                result["body"] = account.body(record)
            except MailError as error:
                result["body"] = record["preview"]
                result["body_error"] = f"the full text is not available ({error}); the body is the preview"
        else:
            result["body"] = record["preview"]
        return result

    def op_attachments(self, request: dict[str, Any]) -> dict[str, Any]:
        record = self._record(request.get("id"))
        return {"id": record["id"], "subject": record["subject"], "attachments": attachments(record)}

    def _attachment_data(self, request: dict[str, Any]) -> tuple[dict[str, Any], int, Any, bytes]:
        record = self._record(request.get("id"))
        index = int(request.get("index", -1))
        part = attachment_part(record, index)
        data = self._account(record["account"]).fetch_part(record, part)
        return record, index, part, data

    def op_export_attachment(self, request: dict[str, Any]) -> dict[str, Any]:
        record = self._record(request.get("id"))
        index = int(request.get("index", -1))
        part = attachment_part(record, index)
        directory = self.cfg.export_dir / record["id"]
        destination = directory / f"{index}-{mime.safe_filename(part.filename, index, part.content_type)}"
        if destination.is_file():
            cached = True
            size = destination.stat().st_size
        else:
            data = self._account(record["account"]).fetch_part(record, part)
            directory.mkdir(mode=0o750, parents=True, exist_ok=True)
            _write_new(destination, data, 0o640)
            cached, size = False, len(data)
        return {"path": str(destination), "filename": part.filename or destination.name, "mime_type": part.content_type,
                "size": size, "cached": cached}

    def op_extract_attachment(self, request: dict[str, Any]) -> dict[str, Any]:
        root = self.cfg.extract_root
        if root is None:
            raise RequestError("extract-attachment is off; use export-attachment")
        root = root.resolve()
        output = Path(str(request.get("output") or "")).expanduser()
        destination = (output if output.is_absolute() else root / output).resolve()
        if destination == root or not destination.is_relative_to(root):
            raise RequestError(f"the output path must be below {root}")
        if destination.exists():
            raise RequestError(f"the output path exists: {destination}")
        record, index, part, data = self._attachment_data(request)
        destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        _write_new(destination, data, 0o640)
        return {"path": str(destination), "filename": part.filename or destination.name, "mime_type": part.content_type, "size": len(data)}

    def op_mark(self, request: dict[str, Any]) -> dict[str, Any]:
        ids = request.get("ids")
        if not isinstance(ids, list) or not ids:
            raise RequestError("ids must be a non-empty list of mail IDs")
        read = bool(request.get("read", True))
        results: dict[str, dict[str, Any]] = {}
        by_account: dict[str, list[dict[str, Any]]] = {}
        for identifier in ids:
            record = self.store.get(str(identifier))
            if record is None:
                results[str(identifier)] = {"ok": False, "error": "message not found"}
            else:
                by_account.setdefault(record["account"], []).append(record)
        for name, records in by_account.items():
            try:
                results.update(self._account(name).set_read(records, read))
            except MailError as error:
                results.update({record["id"]: {"ok": False, "error": str(error)} for record in records})
        failed = {key: value for key, value in results.items() if not value.get("ok")}
        response: dict[str, Any] = {"results": results}
        if failed:
            response["ok"] = False
            response["error"] = "; ".join(f"{key}: {value.get('error', 'failed')}" for key, value in failed.items())
        return response

    def op_archive(self, request: dict[str, Any]) -> dict[str, Any]:
        ids = request.get("ids")
        if not isinstance(ids, list) or not ids:
            raise RequestError("ids must be a non-empty list of mail IDs")
        results: dict[str, dict[str, Any]] = {}
        by_account: dict[str, list[dict[str, Any]]] = {}
        for identifier in ids:
            record = self.store.get(str(identifier))
            if record is None:
                results[str(identifier)] = {"ok": False, "error": "message not found"}
            else:
                by_account.setdefault(record["account"], []).append(record)
        for name, records in by_account.items():
            try:
                results.update(self._account(name).archive(records))
            except MailError as error:
                results.update({record["id"]: {"ok": False, "error": str(error)} for record in records})
        failed = {key: value for key, value in results.items() if not value.get("ok")}
        response: dict[str, Any] = {"results": results}
        if failed:
            response["ok"] = False
            response["error"] = "; ".join(f"{key}: {value.get('error', 'failed')}" for key, value in failed.items())
        return response

    def op_events(self, request: dict[str, Any]) -> dict[str, Any]:
        timeout = min(max(float(request.get("timeout", 0)), 0.0), MAX_WAIT)
        return {"events": self.store.wait_events(timeout) if timeout else self.store.pending_events()}

    def op_ack(self, request: dict[str, Any]) -> dict[str, Any]:
        seqs = request.get("seqs")
        if not isinstance(seqs, list):
            raise RequestError("seqs must be a list of event numbers")
        return {"acked": self.store.ack_events(seqs)}

    def op_auth_begin(self, request: dict[str, Any]) -> dict[str, Any]:
        account = self._account(str(request.get("account") or ""))
        if not account.session:
            raise RequestError(f"account {account.name} uses a password, not OAuth")
        return self.logins.begin(account.name, account.provider, account.cfg.address)

    def op_auth_finish(self, request: dict[str, Any]) -> dict[str, Any]:
        account = self._account(str(request.get("account") or ""))
        if not account.session:
            raise RequestError(f"account {account.name} uses a password, not OAuth")
        self.logins.finish(account.name, account.provider, str(request.get("redirect") or ""))
        account.session.forget()
        account.wake()
        return {"account": account.name, "message": "signed in; the account starts to sync in a few seconds"}

    def handle(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "the request must be a JSON object"}
        operation = getattr(self, f"op_{str(request.get('op', ''))}", None)
        if operation is None:
            return {"ok": False, "error": f"unknown operation {request.get('op')!r}"}
        try:
            result = operation(request)
        except (RequestError, MailError, auth.AuthError, auth.TransientAuthError) as error:
            return {"ok": False, "error": str(error)}
        except (KeyError, TypeError, ValueError) as error:
            return {"ok": False, "error": f"bad request: {error}"}
        except Exception as error:  # noqa: BLE001 - one bad request must not stop the service
            log.exception("request %s failed", request.get("op"))
            return {"ok": False, "error": f"internal error: {type(error).__name__}: {error}"}
        return {"ok": True, **result}


def _write_new(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


class _Handler(socketserver.StreamRequestHandler):
    service: Service

    def handle(self) -> None:
        line = self.rfile.readline(REQUEST_LIMIT + 1)
        if len(line) > REQUEST_LIMIT or not line.endswith(b"\n"):
            response = {"ok": False, "error": "the request is too large or has no newline"}
        else:
            try:
                response = self.service.handle(json.loads(line))
            except ValueError:
                response = {"ok": False, "error": "the request is not JSON"}
        try:
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass


class SocketServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, service: Service, mode: int = 0o660):
        if path.exists() or path.is_symlink():
            if not path.is_socket():
                raise SystemExit(f"{path} exists and is not a socket")
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = type("Handler", (_Handler,), {"service": service})
        old = os.umask(0o777 & ~mode)
        try:
            super().__init__(str(path), handler)
        finally:
            os.umask(old)
        self.path = path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-maild", description="IMAP index service for Hermes")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default=os.environ.get("HERMES_MAIL_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr, format="%(levelname)s %(message)s")
    if sys.version_info < (3, 14):
        log.error("hermes-maild needs Python 3.14 or later for IMAP IDLE")
        return 1
    try:
        cfg = config.load(args.config)
    except config.ConfigError as error:
        log.error("%s", error)
        return 1
    service = Service(cfg)
    server = SocketServer(cfg.socket, service)

    def shutdown(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    service.start()
    log.info("hermes-maild: %d account(s), socket %s", len(service.accounts), cfg.socket)
    try:
        server.serve_forever()
    finally:
        service.stop()
        server.server_close()
        cfg.socket.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
