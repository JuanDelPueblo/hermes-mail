"""hermes-mail: the command line client for hermes-maild.

The command names and arguments match the old Thunderbird bridge, so the
existing prompts and skills continue to work. Each command prints JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .client import Client, MailServiceError


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=None))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hermes-mail", description="Read and manage recent mail through hermes-maild")
    result.add_argument("--socket", default="", help="the service socket (default: $HERMES_MAIL_SOCKET or /run/hermes-mail/mail.sock)")
    sub = result.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show the accounts, their sign-in state and the last sync")

    for name in ("list", "search"):
        command = sub.add_parser(name, help="list recent mail" if name == "list" else "search sender, subject and preview")
        if name == "search":
            command.add_argument("query")
        command.add_argument("--account", default="")
        command.add_argument("--since", help="for example 2d, 12h, yesterday or 2026-09-01")
        command.add_argument("--until")
        command.add_argument("--unread", action="store_true")
        command.add_argument("--limit", type=int, default=50)

    show = sub.add_parser("show", help="show one message with its text")
    show.add_argument("mail_id")
    show.add_argument("--preview", action="store_true", help="show only the stored preview; do not download the text")

    attachments = sub.add_parser("attachments", help="list the attachments of one message")
    attachments.add_argument("mail_id")

    export = sub.add_parser("export-attachment", help="save an attachment in the export cache and print its path")
    export.add_argument("mail_id")
    export.add_argument("attachment_index", type=int)

    extract = sub.add_parser("extract-attachment", help="save an attachment at a path below the extract root")
    extract.add_argument("mail_id")
    extract.add_argument("attachment_index", type=int)
    extract.add_argument("output_path")

    for name in ("mark-read", "mark-unread"):
        command = sub.add_parser(name, help=f"{name.replace('-', ' ')} on the server")
        command.add_argument("mail_ids", nargs="+")

    sub.add_parser("events", help="show the new-mail events that the notifier has not finished")

    auth = sub.add_parser("auth", help="sign in to an OAuth account")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser("login", help="sign in with a browser")
    login.add_argument("account")
    login.add_argument("--redirect", help="the redirect URL; without it, the command asks for it")
    return result


def login(client: Client, account: str, redirect: str | None) -> dict[str, Any]:
    begin = client.auth_begin(account)
    if redirect is None:
        print("Open this URL in a browser and sign in:\n", file=sys.stderr)
        print(begin["url"], file=sys.stderr)
        print(
            f"\nAfter the sign-in, the browser goes to {begin['redirect_uri']}/?code=..."
            "\nThe page does not load. This is expected."
            "\nCopy the full URL from the address bar and paste it here.\n",
            file=sys.stderr,
        )
        redirect = input("Redirect URL: ")
    return client.auth_finish(account, redirect)


def run(args: argparse.Namespace) -> Any:
    client = Client(args.socket)
    command = args.command
    if command == "status":
        return client.status()
    if command in ("list", "search"):
        return client.list(
            account=args.account, since=args.since, until=args.until, unread=args.unread,
            query=getattr(args, "query", ""), limit=args.limit,
        )
    if command == "show":
        return client.show(args.mail_id, full=not args.preview)
    if command == "attachments":
        return client.attachments(args.mail_id)
    if command == "export-attachment":
        return client.export_attachment(args.mail_id, args.attachment_index)
    if command == "extract-attachment":
        return client.extract_attachment(args.mail_id, args.attachment_index, args.output_path)
    if command in ("mark-read", "mark-unread"):
        return client.mark(args.mail_ids, command == "mark-read")
    if command == "events":
        return {"events": client.events()}
    if command == "auth":
        return login(client, args.account, args.redirect)
    raise SystemExit(f"unknown command {command}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except MailServiceError as error:
        print(f"hermes-mail: {error}", file=sys.stderr)
        response = getattr(error, "response", None)
        if response and "results" in response:
            _print(response)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("hermes-mail: cancelled", file=sys.stderr)
        return 1
    result.pop("ok", None)
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
