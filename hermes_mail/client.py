"""Socket client for hermes-maild. The CLI and the Hermes plugin use it.

This module must also run on the Python of Hermes, so it uses only the
standard library and no Python 3.14 features.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Dict, List, Optional

DEFAULT_SOCKET = "/run/hermes-mail/mail.sock"
RESPONSE_LIMIT = 64 * 1024 * 1024


class MailServiceError(Exception):
    """The service is not reachable or it refused the request."""


def socket_path(configured: str = "") -> str:
    return configured or os.environ.get("HERMES_MAIL_SOCKET", "") or DEFAULT_SOCKET


class Client:
    def __init__(self, path: str = "", timeout: float = 120.0):
        self.path = socket_path(path)
        self.timeout = timeout

    def request(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout or self.timeout)
            try:
                connection.connect(self.path)
            except (FileNotFoundError, ConnectionRefusedError) as error:
                raise MailServiceError(f"the mail service is not running (no listener on {self.path})") from error
            except PermissionError as error:
                raise MailServiceError(f"no permission to use the mail service socket {self.path}") from error
            try:
                connection.sendall(json.dumps(payload).encode() + b"\n")
                chunks: List[bytes] = []
                total = 0
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > RESPONSE_LIMIT:
                        raise MailServiceError("the mail service response is too large")
                    if chunk.endswith(b"\n"):
                        break
            except socket.timeout as error:
                raise MailServiceError(f"the mail service did not answer in {timeout or self.timeout:.0f} s") from error
        if not chunks:
            raise MailServiceError("the mail service closed the connection without an answer")
        try:
            response = json.loads(b"".join(chunks))
        except ValueError as error:
            raise MailServiceError("the mail service sent a response that is not JSON") from error
        if not response.get("ok"):
            error = MailServiceError(response.get("error") or "the mail request failed")
            error.response = response  # type: ignore[attr-defined]
            raise error
        return response

    # One method for each operation.

    def status(self) -> Dict[str, Any]:
        return self.request({"op": "status"})

    def accounts(self) -> Dict[str, Any]:
        return self.request({"op": "accounts"})["accounts"]

    def folders(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "folders", "account": account})

    def list(self, **filters: Any) -> Dict[str, Any]:
        return self.request({"op": "list", **{key: value for key, value in filters.items() if value not in (None, "", False)}})

    def show(self, mail_id: str, full: bool = True) -> Dict[str, Any]:
        return self.request({"op": "show", "id": mail_id, "full": full})

    def attachments(self, mail_id: str) -> Dict[str, Any]:
        return self.request({"op": "attachments", "id": mail_id})

    def export_attachment(self, mail_id: str, index: int) -> Dict[str, Any]:
        return self.request({"op": "export_attachment", "id": mail_id, "index": index})

    def extract_attachment(self, mail_id: str, index: int, output: str) -> Dict[str, Any]:
        return self.request({"op": "extract_attachment", "id": mail_id, "index": index, "output": output})

    def mark(self, mail_ids: List[str], read: bool) -> Dict[str, Any]:
        return self.request({"op": "mark", "ids": list(mail_ids), "read": read})

    def archive(self, mail_ids: List[str]) -> Dict[str, Any]:
        return self.request({"op": "archive", "ids": list(mail_ids)})

    def events(self, timeout: float = 0) -> List[Dict[str, Any]]:
        return self.request({"op": "events", "timeout": timeout}, timeout=timeout + 30)["events"]

    def ack(self, seqs: List[int]) -> int:
        return self.request({"op": "ack", "seqs": list(seqs)})["acked"]

    # The triage log.

    def triage_record(self, entry: Dict[str, Any], event_seq: Optional[int] = None) -> int:
        payload: Dict[str, Any] = {"op": "triage_record", "entry": entry}
        if event_seq is not None:
            payload["event_seq"] = int(event_seq)
        return self.request(payload)["id"]

    def triage_update(self, entry_id: int, fields: Dict[str, Any]) -> None:
        self.request({"op": "triage_update", "id": entry_id, "fields": fields})

    def triage_list(self, **filters: Any) -> Dict[str, Any]:
        return self.request({"op": "triage_list", **{key: value for key, value in filters.items() if value not in (None, "")}})

    def triage_show(self, entry_id: int) -> Dict[str, Any]:
        return self.request({"op": "triage_show", "id": int(entry_id)})

    def triage_retry(self, entry_id: int) -> Dict[str, Any]:
        return self.request({"op": "triage_retry", "id": int(entry_id)})

    def auth_begin(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "auth_begin", "account": account})

    def auth_finish(self, account: str, redirect: str) -> Dict[str, Any]:
        return self.request({"op": "auth_finish", "account": account, "redirect": redirect})

    def settings(self) -> Dict[str, Any]:
        return self.request({"op": "settings"})

    def settings_save(self, account: str, settings: Dict[str, Any], password: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"op": "settings_save", "account": account, "settings": settings}
        if password is not None:
            payload["password"] = password
        return self.request(payload)

    def settings_delete(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "settings_delete", "account": account})

    def settings_reset(self, account: str) -> Dict[str, Any]:
        return self.request({"op": "settings_reset", "account": account})
