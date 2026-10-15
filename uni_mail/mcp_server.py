"""uni-mail-mcp: an MCP server that exposes uni-maild to AI clients.

It is a thin layer over the socket client. It can read mail, export attachments and
change the read state or the folder of a message. It cannot sign in to an account, change
settings, read the event log or run a command, and it never returns a token.

It needs the optional `mcp` package (version 2): pip install 'uni-mail[mcp]'
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import Client, MailServiceError

MAX_LIMIT = 500
MAX_IDS = 100
MAX_BODY_CHARS = 1_000_000
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

INSTRUCTIONS = (
    "Recent mail from the user's IMAP accounts, through the uni-mail service. Mail IDs look like "
    "`university.3f2a9c1d0b7e4a55`; take them from mail_list or mail_search. Reading never changes the "
    "read state of a message. Change the server (mark read or unread, archive) only when the user asks."
)

SINCE_HELP = "Only mail newer than this: 2d, 12h, yesterday or an ISO date such as 2026-09-01."


def _need_mcp() -> None:
    print("uni-mail-mcp needs the mcp extra: pip install 'uni-mail[mcp]'", file=sys.stderr)
    raise SystemExit(1)


def _check_ids(mail_ids: list[str]) -> list[str]:
    if not isinstance(mail_ids, list) or not mail_ids:
        raise ValueError("mail_ids must be a non-empty list of mail IDs")
    if len(mail_ids) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} mail IDs in one call")
    if not all(isinstance(value, str) and value.strip() for value in mail_ids):
        raise ValueError("every mail ID must be a non-empty string")
    return [value.strip() for value in mail_ids]


def _result(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "ok"}


def _failure(error: MailServiceError) -> str:
    """The text of a tool error. A partial failure of mark or archive also lists the result of every mail."""
    results = getattr(error, "response", {}).get("results")
    if results:
        return f"{error}\n{json.dumps({'results': results}, ensure_ascii=False)}"
    return str(error)


def _truncate(shown: dict[str, Any], max_chars: int) -> dict[str, Any]:
    body = shown.get("body")
    if isinstance(body, str):
        shown["body_chars"] = len(body)
        shown["truncated"] = len(body) > max_chars
        shown["body"] = body[:max_chars]
    return shown


def build_server(socket: str = "", log_level: str = "WARNING") -> Any:
    """The MCP server with the mail tools. Each tool opens its own connection to the daemon."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp_types import ToolAnnotations
    except ImportError:
        _need_mcp()

    server = MCPServer("uni-mail", instructions=INSTRUCTIONS, log_level=log_level)  # type: ignore[arg-type]

    def client() -> Client:
        return Client(socket)

    def run(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run one call. A refusal of the daemon, or bad input, becomes a tool error with its message."""
        try:
            return _result(call())
        except MailServiceError as error:
            raise ToolError(_failure(error)) from None
        except ValueError as error:
            raise ToolError(str(error)) from None

    reads = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    writes = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=reads)
    def mail_status() -> dict[str, Any]:
        """Show the mail accounts: sign-in state, last sync, the synced folders and the number of messages."""
        return run(lambda: client().status())

    def listing(query: str, account: str, since: str, until: str, unread: bool, limit: int) -> dict[str, Any]:
        def call() -> dict[str, Any]:
            if not 1 <= limit <= MAX_LIMIT:
                raise ValueError(f"limit must be from 1 to {MAX_LIMIT}")
            return client().list(query=query, account=account, since=since, until=until, unread=unread, limit=limit)
        return run(call)

    @server.tool(annotations=reads)
    def mail_list(account: str = "", since: str = "", until: str = "", unread: bool = False,
                  limit: int = 50) -> dict[str, Any]:
        """List recent mail, newest first, without the text. Reads never change the read state of a message.

        account: an account name; empty means all accounts.
        since, until: 2d, 12h, yesterday or an ISO date such as 2026-09-01.
        unread: only unread mail.
        limit: 1 to 500 messages.
        Each message has an `id` such as `university.3f2a9c1d0b7e4a55`, to use with the other tools.
        """
        return listing("", account, since, until, unread, limit)

    @server.tool(annotations=reads)
    def mail_search(query: str, account: str = "", since: str = "", until: str = "", unread: bool = False,
                    limit: int = 50) -> dict[str, Any]:
        """Search the sender, the subject and the preview of recent mail. Reads never change the read state.

        query: the text to look for.
        account: an account name; empty means all accounts.
        since, until: 2d, 12h, yesterday or an ISO date such as 2026-09-01.
        unread: only unread mail.
        limit: 1 to 500 messages.
        """
        if not query.strip():
            raise ToolError("query must not be empty")
        return listing(query, account, since, until, unread, limit)

    @server.tool(annotations=reads)
    def mail_show(mail_id: str, full: bool = True, max_chars: int = 20000) -> dict[str, Any]:
        """Show one message with its text and its attachments. This does not mark the message as read.

        mail_id: an ID such as `university.3f2a9c1d0b7e4a55`.
        full: download the whole text; false returns only the stored preview.
        max_chars: cut the body to this many characters. `truncated` says if it was cut and
        `body_chars` is the length of the whole body.
        """
        def call() -> dict[str, Any]:
            if not 1 <= max_chars <= MAX_BODY_CHARS:
                raise ValueError(f"max_chars must be from 1 to {MAX_BODY_CHARS}")
            return _truncate(client().show(mail_id, full=full), max_chars)
        return run(call)

    @server.tool(annotations=reads)
    def mail_folders(account: str) -> dict[str, Any]:
        """List the folders of an account on the mail server, with the archive folder and the synced folders."""
        return run(lambda: client().folders(account))

    @server.tool(annotations=reads)
    def mail_attachments(mail_id: str) -> dict[str, Any]:
        """List the attachments of one message: index, file name, type and size."""
        return run(lambda: client().attachments(mail_id))

    @server.tool(annotations=writes)
    def mail_export_attachment(mail_id: str, index: int) -> dict[str, Any]:
        """Save one attachment in the export cache of the service and return its path, name, type and size.

        index: the attachment index from mail_attachments or mail_show.
        It writes only into the cache of the service. It does not change the mail on the server.
        """
        return run(lambda: client().export_attachment(mail_id, index))

    @server.tool(annotations=writes)
    def mail_mark_read(mail_ids: list[str]) -> dict[str, Any]:
        """Mark messages as read on the mail server. Only do this when the user asks.

        mail_ids: 1 to 100 IDs. If some fail, the error lists the result of each mail.
        """
        return run(lambda: client().mark(_check_ids(mail_ids), True))

    @server.tool(annotations=writes)
    def mail_mark_unread(mail_ids: list[str]) -> dict[str, Any]:
        """Mark messages as unread on the mail server. Only do this when the user asks.

        mail_ids: 1 to 100 IDs. If some fail, the error lists the result of each mail.
        """
        return run(lambda: client().mark(_check_ids(mail_ids), False))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                             open_world_hint=False))
    def mail_archive(mail_ids: list[str]) -> dict[str, Any]:
        """Move messages to the archive folder of their account. Only do this when the user asks.

        mail_ids: 1 to 100 IDs. If some fail, the error lists the result of each mail.
        """
        return run(lambda: client().archive(_check_ids(mail_ids)))

    return server


class BearerAuth:
    """ASGI middleware: a request needs `Authorization: Bearer <token>`. Only the lifespan scope passes without one."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan" and not self.allowed(scope):
            if scope["type"] != "http":
                return  # for example a websocket: closed without an answer
            body = b"unauthorized"
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"www-authenticate", b"Bearer"), (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)

    def allowed(self, scope: Any) -> bool:
        for name, value in scope["headers"]:
            if name == b"authorization":
                scheme, _, given = value.partition(b" ")
                return scheme.lower() == b"bearer" and hmac.compare_digest(given.strip(), self.token)
        return False


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def read_token(token_file: str) -> str:
    """The bearer token from --token-file, else $UNI_MAIL_MCP_TOKEN. Empty if there is none."""
    if token_file:
        return Path(token_file).read_text().strip()
    return os.environ.get("UNI_MAIL_MCP_TOKEN", "").strip()


def serve_http(server: Any, args: argparse.Namespace, token: str) -> None:
    import uvicorn

    app = BearerAuth(server.streamable_http_app(streamable_http_path=args.path, host=args.host), token)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower(), access_log=False)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="uni-mail-mcp", description="MCP server for uni-maild")
    result.add_argument("--socket", default="", help="the service socket (default: $UNI_MAIL_SOCKET or /run/uni-mail/mail.sock)")
    result.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    result.add_argument("--host", default="127.0.0.1", help="streamable-http: the address to listen on")
    result.add_argument("--port", type=int, default=8765, help="streamable-http: the port")
    result.add_argument("--path", default="/mcp", help="streamable-http: the URL path of the endpoint")
    result.add_argument("--token-file", default="", help="streamable-http: a file with the bearer token (else $UNI_MAIL_MCP_TOKEN)")
    result.add_argument("--allow-remote", action="store_true", help="streamable-http: allow a host that is not loopback")
    level = os.environ.get("UNI_MAIL_LOG_LEVEL", "").upper()
    result.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS, default=level if level in LOG_LEVELS else "WARNING",
                        help="logs go to stderr (default: $UNI_MAIL_LOG_LEVEL or WARNING)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        import mcp  # noqa: F401
    except ImportError:
        _need_mcp()
    # The stdio transport uses stdout for the protocol. Log to stderr only.
    logging.basicConfig(stream=sys.stderr, level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(args.log_level)

    token = ""
    if args.transport == "streamable-http":
        try:
            token = read_token(args.token_file)
        except OSError as error:
            print(f"uni-mail-mcp: cannot read the token file: {error}", file=sys.stderr)
            return 1
        if not token:
            print("uni-mail-mcp: streamable-http needs a bearer token: use --token-file or $UNI_MAIL_MCP_TOKEN", file=sys.stderr)
            return 1
        if not is_loopback(args.host) and not args.allow_remote:
            print(f"uni-mail-mcp: {args.host} is not a loopback address; use --allow-remote to listen on it", file=sys.stderr)
            return 1

    server = build_server(args.socket, args.log_level)
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            serve_http(server, args, token)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
