"""A small in-process IMAP server and OAuth token endpoint for tests.

The IMAP server implements only the commands that hermes-mail uses. It
records every command, so a test can check what the client asked for. A FETCH
of BODY[...] without PEEK sets \\Seen, as a real server does.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
import re
import select
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

MONTHS = {name: number for number, name in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


@dataclass
class Message:
    uid: int
    received: date
    subject: str
    body: str = "Body text.\r\n"
    flags: set[str] = field(default_factory=set)
    html: str = ""
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)
    sender: str = "Sender <sender@example.com>"
    _raw: bytes = b""

    def raw(self) -> bytes:
        if self._raw:
            return self._raw
        message = EmailMessage(policy=email.policy.SMTP)
        message["Date"] = f"{self.received.strftime('%a, %d %b %Y')} 10:00:00 +0000"
        message["From"] = self.sender
        message["To"] = "student@example.edu"
        message["Subject"] = self.subject
        message["Message-ID"] = f"<{self.uid}@example.com>"
        if not self.html and not self.attachments:
            # A single part, as simple notices use.
            message.set_content(self.body)
        else:
            message.set_content(self.body)
            if self.html:
                message.add_alternative(self.html, subtype="html")
            for name, content_type, data in self.attachments:
                main, sub = content_type.split("/", 1)
                message.add_attachment(data, maintype=main, subtype=sub, filename=name)
        self._raw = message.as_bytes()
        return self._raw

    def parsed(self) -> email.message.Message:
        return email.message_from_bytes(self.raw(), policy=email.policy.compat32)

    def header_fields(self, names: list[str]) -> bytes:
        wanted = {name.lower() for name in names}
        head = self.raw().split(b"\r\n\r\n", 1)[0].split(b"\r\n")
        lines = [line for line in head if line.split(b":", 1)[0].decode().lower() in wanted]
        return b"\r\n".join(lines) + b"\r\n\r\n"

    def part(self, number: str) -> bytes:
        node = self.parsed()
        for index in number.split("."):
            if node.is_multipart():
                node = node.get_payload()[int(index) - 1]
            elif index != "1":
                raise KeyError(number)
        payload = node.get_payload(decode=False)
        return payload.encode() if isinstance(payload, str) else b""

    def bodystructure(self) -> str:
        return _structure(self.parsed())


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _structure(node: email.message.Message) -> str:
    if node.is_multipart():
        children = "".join(_structure(child) for child in node.get_payload())
        return f"({children} {_quote(node.get_content_subtype().upper())})"
    main, sub = node.get_content_maintype().upper(), node.get_content_subtype().upper()
    params = []
    for name, value in node.get_params()[1:] if node.get_params() else []:
        params += [_quote(name.upper()), _quote(value)]
    param_text = f"({' '.join(params)})" if params else "NIL"
    payload = node.get_payload(decode=False)
    body = payload.encode() if isinstance(payload, str) else b""
    encoding = (node.get("Content-Transfer-Encoding") or "7BIT").upper()
    content_id = _quote(node["Content-ID"]) if node["Content-ID"] else "NIL"
    text = f"({_quote(main)} {_quote(sub)} {param_text} {content_id} NIL {_quote(encoding)} {len(body)}"
    if main == "TEXT":
        text += f" {body.count(b'\n')}"
    disposition = node.get_content_disposition()
    if disposition:
        filename = node.get_filename()
        disposition_params = f"({_quote('FILENAME')} {_quote(filename)})" if filename else "NIL"
        text += f" NIL ({_quote(disposition.upper())} {disposition_params})"
    return text + ")"


@dataclass
class Mailbox:
    user: str
    token: str = ""
    password: str = ""
    messages: list[Message] = field(default_factory=list)
    idle_event_delay: float | None = None
    commands: list[str] = field(default_factory=list)
    uidvalidity: int = 1
    capabilities: str = "IMAP4rev1 IDLE UIDPLUS UNSELECT AUTH=XOAUTH2 AUTH=PLAIN"
    folders: dict[str, list[Message]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    wake: threading.Event = field(default_factory=threading.Event)
    idling: threading.Event = field(default_factory=threading.Event)

    def folder(self, name: str) -> list[Message]:
        if name.upper() == "INBOX":
            return self.messages
        return self.folders.setdefault(name, [])

    def deliver(self, message: Message, folder: str = "INBOX") -> None:
        with self.lock:
            self.folder(folder).append(message)
        self.wake.set()


def parse_set(text: str, known: list[int]) -> list[int]:
    result: list[int] = []
    top = max(known, default=0)
    for part in text.split(","):
        if ":" in part:
            low, high = (top if value == "*" else int(value) for value in part.split(":"))
            low, high = min(low, high), max(low, high)
            result.extend(uid for uid in known if low <= uid <= high)
        else:
            uid = top if part == "*" else int(part)
            if uid in known:
                result.append(uid)
    return result


FETCH_ITEM = re.compile(r"BODY(\.PEEK)?\[([^\]]*)\](?:<(\d+)\.(\d+)>)?|[A-Z0-9.]+", re.IGNORECASE)


class Handler(socketserver.StreamRequestHandler):
    mailbox: Mailbox

    def send(self, line: str | bytes) -> None:
        self.wfile.write((line if isinstance(line, bytes) else line.encode()) + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        self.selected = "INBOX"
        self.readonly = True
        self.send("* OK fake IMAP ready")
        while line := self.rfile.readline():
            text = line.decode().rstrip("\r\n")
            self.mailbox.commands.append(text)
            tag, _, rest = text.partition(" ")
            command, _, arguments = rest.partition(" ")
            handler = getattr(self, f"do_{command.upper()}", None)
            if handler is None:
                self.send(f"{tag} BAD unknown command")
                continue
            with self.mailbox.lock if command.upper() != "IDLE" else _nolock():
                if handler(tag, arguments) is False:
                    return

    @property
    def messages(self) -> list[Message]:
        return self.mailbox.folder(self.selected)

    def do_CAPABILITY(self, tag: str, _: str) -> None:
        self.send(f"* CAPABILITY {self.mailbox.capabilities}")
        self.send(f"{tag} OK CAPABILITY completed")

    def do_NOOP(self, tag: str, _: str) -> None:
        self.send(f"{tag} OK NOOP completed")

    def do_LOGIN(self, tag: str, arguments: str) -> None:
        user, password = (value.strip('"') for value in arguments.split(" ", 1))
        if self.mailbox.password and user == self.mailbox.user and password == self.mailbox.password:
            self.send(f"{tag} OK LOGIN completed")
        else:
            self.send(f"{tag} NO LOGIN failed")

    def do_AUTHENTICATE(self, tag: str, arguments: str) -> None:
        if arguments.upper() != "XOAUTH2":
            self.send(f"{tag} NO unsupported mechanism")
            return
        self.send("+ ")
        response = base64.b64decode(self.rfile.readline().strip()).decode()
        expected = f"user={self.mailbox.user}\x01auth=Bearer {self.mailbox.token}\x01\x01"
        if response == expected:
            self.send(f"{tag} OK AUTHENTICATE completed")
            return
        error = base64.b64encode(json.dumps({"status": "401", "schemes": "bearer"}).encode()).decode()
        self.send(f"+ {error}")
        self.rfile.readline()
        self.send(f"{tag} NO AUTHENTICATE failed")

    def do_STATUS(self, tag: str, arguments: str) -> None:
        messages = self.mailbox.folder(arguments.split(" ", 1)[0].strip('"'))
        unseen = sum(1 for message in messages if "\\Seen" not in message.flags)
        uidnext = max((message.uid for message in messages), default=0) + 1
        self.send(f"* STATUS INBOX (MESSAGES {len(messages)} UNSEEN {unseen} "
                  f"UIDVALIDITY {self.mailbox.uidvalidity} UIDNEXT {uidnext})")
        self.send(f"{tag} OK STATUS completed")

    def select(self, tag: str, arguments: str, readonly: bool) -> None:
        self.selected = arguments.strip().strip('"')
        self.readonly = readonly
        self.send(f"* {len(self.messages)} EXISTS")
        self.send(f"* OK [UIDVALIDITY {self.mailbox.uidvalidity}] UIDs valid")
        self.send(f"{tag} OK [{'READ-ONLY' if readonly else 'READ-WRITE'}] done")

    def do_EXAMINE(self, tag: str, arguments: str) -> None:
        self.select(tag, arguments, readonly=True)

    def do_SELECT(self, tag: str, arguments: str) -> None:
        self.select(tag, arguments, readonly=False)

    def do_UNSELECT(self, tag: str, _: str) -> None:
        self.send(f"{tag} OK UNSELECT completed")

    def do_CLOSE(self, tag: str, _: str) -> None:
        self.send(f"{tag} OK CLOSE completed")

    def do_UID(self, tag: str, arguments: str) -> None:
        command, _, rest = arguments.partition(" ")
        getattr(self, f"uid_{command.upper()}")(tag, rest)

    def uid_SEARCH(self, tag: str, arguments: str) -> None:
        match = re.fullmatch(r"SINCE (\d+)-(\w{3})-(\d{4})", arguments)
        since = date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
        uids = [str(message.uid) for message in self.messages if message.received >= since]
        self.send("* SEARCH" + "".join(f" {uid}" for uid in uids))
        self.send(f"{tag} OK SEARCH completed")

    def uid_FETCH(self, tag: str, arguments: str) -> None:
        uid_set, _, items = arguments.partition(" ")
        by_uid = {message.uid: message for message in self.messages}
        requested = [match for match in FETCH_ITEM.finditer(items.strip("()"))]
        for uid in parse_set(uid_set, sorted(by_uid)):
            message = by_uid[uid]
            sequence = self.messages.index(message) + 1
            out = bytearray(f"* {sequence} FETCH (UID {uid}".encode())
            for match in requested:
                name = match.group(0).upper()
                if name == "UID":
                    continue
                if name == "FLAGS":
                    out += f" FLAGS ({' '.join(sorted(message.flags))})".encode()
                elif name == "RFC822.SIZE":
                    out += f" RFC822.SIZE {len(message.raw())}".encode()
                elif name == "INTERNALDATE":
                    out += f' INTERNALDATE "{message.received.strftime("%d-%b-%Y")} 10:00:00 +0000"'.encode()
                elif name == "BODYSTRUCTURE":
                    out += f" BODYSTRUCTURE {message.bodystructure()}".encode()
                elif name in ("RFC822", "BODY[]"):
                    message.flags.add("\\Seen")
                    data = message.raw()
                    out += f" RFC822 {{{len(data)}}}\r\n".encode() + data
                elif match.group(2) is not None:
                    section_name = match.group(2)
                    if not match.group(1):
                        message.flags.add("\\Seen")
                    if section_name.upper().startswith("HEADER.FIELDS"):
                        data = message.header_fields(re.search(r"\(([^)]*)\)", section_name).group(1).split())
                    elif section_name == "":
                        data = message.raw()
                    else:
                        data = message.part(section_name)
                    label = f"BODY[{section_name}]"
                    if match.group(3) is not None:
                        start, length = int(match.group(3)), int(match.group(4))
                        data = data[start:start + length]
                        label += f"<{start}>"
                    out += f" {label} {{{len(data)}}}\r\n".encode() + data
            out += b")"
            self.send(bytes(out))
        self.send(f"{tag} OK FETCH completed")

    def uid_STORE(self, tag: str, arguments: str) -> None:
        if self.readonly:
            self.send(f"{tag} NO mailbox is read-only")
            return
        uid_set, mode, flags = arguments.split(" ", 2)
        names = set(flags.strip("()").split())
        targets = parse_set(uid_set, [message.uid for message in self.messages])
        for message in self.messages:
            if message.uid in targets:
                if mode.startswith("+"):
                    message.flags |= names
                else:
                    message.flags -= names
        self.send(f"{tag} OK STORE completed")

    def do_IDLE(self, tag: str, _: str) -> None:
        self.send("+ idling")
        self.mailbox.idling.set()
        started = time.monotonic()
        delay_sent = False
        try:
            while True:
                if self.mailbox.wake.is_set():
                    self.mailbox.wake.clear()
                    self.send(f"* {len(self.messages)} EXISTS")
                if (self.mailbox.idle_event_delay is not None and not delay_sent
                        and time.monotonic() - started >= self.mailbox.idle_event_delay):
                    delay_sent = True
                    self.send(f"* {len(self.messages) + 1} EXISTS")
                if not select.select([self.connection], [], [], 0.05)[0]:
                    continue
                line = self.rfile.readline()
                if not line:
                    return
                if line.strip().upper() == b"DONE":
                    break
        finally:
            self.mailbox.idling.clear()
        self.send(f"{tag} OK IDLE terminated")

    def do_LOGOUT(self, tag: str, _: str) -> bool:
        self.send("* BYE logging out")
        self.send(f"{tag} OK LOGOUT completed")
        return False


class _nolock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeIMAPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, mailbox: Mailbox):
        handler = type("BoundHandler", (Handler,), {"mailbox": mailbox})
        super().__init__(("127.0.0.1", 0), handler)
        self.mailbox = mailbox
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class FakeTokenEndpoint(HTTPServer):
    """Accept one code. Each refresh rotates the refresh token."""

    def __init__(self, code: str, access_token: str, error: dict | None = None):
        self.code = code
        self.access_token = access_token
        self.error = error
        self.revoked = False
        self.valid_refresh = {"refresh-1"}
        self.counter = 1
        self.requests: list[dict[str, str]] = []
        endpoint = self

        class TokenHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                fields = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                endpoint.requests.append(fields)
                status, payload = endpoint.respond(fields)
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(("127.0.0.1", 0), TokenHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/token"

    def respond(self, fields: dict[str, str]) -> tuple[int, dict]:
        if self.error:
            return 400, self.error
        if fields.get("grant_type") == "authorization_code" and fields.get("code") == self.code:
            return 200, {"access_token": "first-access", "refresh_token": "refresh-1", "expires_in": 3600, "scope": "imap"}
        if fields.get("grant_type") == "refresh_token" and fields.get("refresh_token") in self.valid_refresh and not self.revoked:
            self.counter += 1
            new = f"refresh-{self.counter}"
            self.valid_refresh.add(new)
            return 200, {"access_token": self.access_token, "refresh_token": new, "expires_in": 3600}
        return 400, {"error": "invalid_grant", "error_description": "AADSTS70000: The code is not valid."}

    def close(self) -> None:
        self.shutdown()
        self.server_close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
