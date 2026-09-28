#!/usr/bin/env python3
"""Phase 0 probe for hermes-mail: can this host read mail over IMAP directly?

The probe signs in to one mail account with the public OAuth client of
Thunderbird, or with a password. Then it runs the IMAP operations that the
hermes-mail service needs and prints a GO or NO GO verdict.

The probe does not download the mailbox. It reads counters, message UIDs,
message sizes and two header fields of a small sample. It reads all data with
BODY.PEEK, so it does not mark mail as read. The only change on the server is
the optional \\Seen test. That test changes the read state of one message and
then sets it back to its initial value.

The probe keeps tokens and passwords in memory only. It never prints or
writes them.

Run it with Python 3.14 or later:

    nix shell nixpkgs#python314 --command python3 scripts/probe.py microsoft --user you@example.edu
    nix shell nixpkgs#python314 --command python3 scripts/probe.py microsoft --user you@outlook.com
    nix shell nixpkgs#python314 --command python3 scripts/probe.py google --user you@gmail.com
    nix shell nixpkgs#python314 --command python3 scripts/probe.py google --user you@gmail.com --auth password

Exit status: 0 for GO, 1 for NO GO, 2 when a required check did not run.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import email.policy
import getpass
import hashlib
import imaplib
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Provider:
    host: str
    port: int
    auth_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    scope: str
    # Thunderbird ships the Google secret in its public source code. Google
    # treats the secret of an installed application as public, so it does not
    # protect anything and is not a credential of this project.
    client_secret: str | None = None
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    tls: bool = True


# These values are the built-in Thunderbird OAuth issuers from
# comm/mailnews/base/src/OAuth2Providers.sys.mjs. The "common" Microsoft
# endpoint accepts both work or school accounts and personal accounts.
PROVIDERS: dict[str, Provider] = {
    "microsoft": Provider(
        host="outlook.office365.com",
        port=993,
        auth_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        redirect_uri="https://localhost",
        scope="https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    ),
    "google": Provider(
        host="imap.gmail.com",
        port=993,
        auth_endpoint="https://accounts.google.com/o/oauth2/auth",
        token_endpoint="https://www.googleapis.com/oauth2/v3/token",
        client_id="406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com",
        client_secret="kSmqreRr0qwBWJgbf5Y-PjSU",
        redirect_uri="http://localhost",
        scope="https://mail.google.com/",
        extra_auth_params={"access_type": "offline"},
    ),
}

# Hints for common Microsoft Entra sign-in errors.
AADSTS_HINTS = {
    "50011": "The redirect URI does not match the app registration.",
    "50076": "The tenant asks for MFA again. Sign in again in the browser.",
    "50079": "The tenant asks for MFA enrollment.",
    "50105": "The tenant allows this app only for assigned users.",
    "53003": "A Conditional Access policy blocks this sign-in.",
    "65001": "The tenant has not given consent to this app for your account.",
    "65004": "You declined the consent prompt.",
    "70000": "The authorization code is not valid or was already used.",
    "700016": "The app does not exist in the tenant.",
    "7000218": "The app requires a client secret. It is not a public client.",
    "90094": "The tenant requires admin consent for this app.",
}

SESSION_CHECKS = ["imap-login", "idle-capability", "window-search", "peek", "idle", "seen-flag"]
OAUTH_CHECKS = ["token", "refresh"]


class ProbeError(Exception):
    """A check failed. The message is the error text to show."""


class CountingIMAP:
    """Count every byte that the server sends to this client."""

    received = 0

    def read(self, size):
        data = super().read(size)
        self.received += len(data)
        return data

    def readline(self):
        line = super().readline()
        self.received += len(line)
        return line


class CountingIMAP4(CountingIMAP, imaplib.IMAP4):
    pass


class CountingIMAP4SSL(CountingIMAP, imaplib.IMAP4_SSL):
    pass


class Results:
    def __init__(self, required: list[str]):
        self.required = required
        self.rows: dict[str, tuple[str, str]] = {}

    def record(self, name: str, status: str, detail: str) -> None:
        self.rows[name] = (status, detail)
        print(f"  [{status}] {name}: {detail}")

    def verdict(self) -> tuple[str, int]:
        statuses = [self.rows.get(name, ("SKIP", ""))[0] for name in self.required]
        if "FAIL" in statuses:
            return "NO GO", 1
        if all(status == "PASS" for status in statuses):
            return "GO", 0
        return "INCOMPLETE", 2

    def summary(self) -> int:
        print("\nSummary")
        for name in self.required:
            status, detail = self.rows.get(name, ("SKIP", "did not run"))
            print(f"  {status:<4}  {name:<16} {detail}")
        verdict, code = self.verdict()
        print(f"\nVerdict: {verdict}")
        return code


def human_size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def token_error_text(payload: dict) -> str:
    error = payload.get("error", "unknown_error")
    description = str(payload.get("error_description", "")).splitlines()
    text = f"{error}: {description[0]}" if description else error
    match = re.search(r"AADSTS(\d+)", text)
    if match and match.group(1) in AADSTS_HINTS:
        text += f" ({AADSTS_HINTS[match.group(1)]})"
    return text


def token_request(provider: Provider, fields: dict[str, str]) -> dict:
    data = {"client_id": provider.client_id, **fields}
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    request = urllib.request.Request(
        provider.token_endpoint,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            payload = json.loads(body)
        except ValueError:
            raise ProbeError(f"HTTP {error.code} from the token endpoint") from None
        raise ProbeError(token_error_text(payload)) from None
    except urllib.error.URLError as error:
        raise ProbeError(f"cannot reach the token endpoint: {error.reason}") from None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(provider: Provider, user: str, state: str, challenge: str | None) -> str:
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "scope": provider.scope,
        "state": state,
        "login_hint": user,
        **provider.extra_auth_params,
    }
    if challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    return f"{provider.auth_endpoint}?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"


def code_from_redirect(text: str, state: str) -> str:
    text = text.strip()
    if "code=" not in text and "error=" not in text:
        if not text:
            raise ProbeError("no redirect URL was given")
        return text
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
    params = urllib.parse.parse_qs(query)
    if "error" in params:
        raise ProbeError(token_error_text({
            "error": params["error"][0],
            "error_description": params.get("error_description", [""])[0],
        }))
    if params.get("state", [None])[0] != state:
        raise ProbeError("the state value in the redirect URL does not match this sign-in")
    if "code" not in params:
        raise ProbeError("the redirect URL has no code parameter")
    return params["code"][0]


def oauth_sign_in(provider: Provider, args: argparse.Namespace, results: Results) -> str:
    """Return an access token. Record the token and refresh checks."""
    state = secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair() if args.pkce else (None, None)
    print("\n1. Open this URL in your browser and sign in:\n")
    print(authorization_url(provider, args.user, state, challenge))
    print(
        f"\nAfter the sign-in, the browser goes to {provider.redirect_uri}/?code=..."
        "\nThe page does not load. This is expected."
        "\nCopy the full URL from the address bar and paste it here."
    )
    print("\n2. Token checks")
    try:
        code = code_from_redirect(input("\nRedirect URL: "), state)
        fields = {"grant_type": "authorization_code", "code": code, "redirect_uri": provider.redirect_uri}
        if verifier:
            fields["code_verifier"] = verifier
        tokens = token_request(provider, fields)
    except ProbeError as error:
        results.record("token", "FAIL", str(error))
        raise
    if "access_token" not in tokens:
        results.record("token", "FAIL", "the token response has no access token")
        raise ProbeError("no access token")
    results.record("token", "PASS", f"access token for {tokens.get('expires_in', '?')} s, scope: {tokens.get('scope', '?')}")

    # The service runs for months on one sign-in, so the refresh grant must
    # work without a browser.
    if "refresh_token" not in tokens:
        results.record("refresh", "FAIL", "the provider gave no refresh token (offline access denied)")
        return tokens["access_token"]
    try:
        refreshed = token_request(provider, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    except ProbeError as error:
        results.record("refresh", "FAIL", str(error))
        return tokens["access_token"]
    rotated = "new refresh token" if refreshed.get("refresh_token", tokens["refresh_token"]) != tokens["refresh_token"] else "same refresh token"
    results.record("refresh", "PASS", f"refresh grant works without the browser ({rotated})")
    return refreshed.get("access_token", tokens["access_token"])


def xoauth2(user: str, token: str):
    sent = False

    def respond(challenge: bytes) -> bytes:
        nonlocal sent
        # A second challenge carries the error details. An empty reply ends
        # the exchange, so the server can send its NO response.
        if sent:
            return b""
        sent = True
        return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()

    return respond


def auth_failure_detail(imap: imaplib.IMAP4, error: Exception) -> str:
    detail = str(error)
    challenge = getattr(imap, "continuation_response", b"")
    if challenge:
        try:
            detail += f" ({base64.b64decode(challenge).decode(errors='replace')})"
        except ValueError:
            pass
    return detail


def compact_set(uids: list[int]) -> str:
    """Return an IMAP sequence set such as 3:7,9."""
    parts: list[str] = []
    ordered = sorted(set(uids))
    start = previous = ordered[0]
    for uid in ordered[1:] + [None]:
        if uid is not None and uid == previous + 1:
            previous = uid
            continue
        parts.append(str(start) if start == previous else f"{start}:{previous}")
        if uid is not None:
            start = previous = uid
    return ",".join(parts)


def imap_date(value: date) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{value.day:02d}-{months[value.month - 1]}-{value.year}"


def check_ok(result: tuple, what: str) -> list:
    typ, data = result
    if typ != "OK":
        raise ProbeError(f"{what} failed: {typ} {data!r}")
    return data


def fetch_rows(data: list) -> list[tuple[bytes, bytes | None]]:
    """Return (prefix, literal) pairs from a FETCH response.

    imaplib removes "* <n> FETCH", so a row starts with "<n> (". A row with a
    literal is a tuple, and a b")" item closes it.
    """
    rows = []
    for item in data:
        if isinstance(item, tuple):
            rows.append((item[0], item[1]))
        elif isinstance(item, bytes) and re.match(rb"\d+ \(", item):
            rows.append((item, None))
    return rows


def fetch_flags(imap: imaplib.IMAP4, uids: list[int]) -> dict[int, set[str]]:
    data = check_ok(imap.uid("FETCH", compact_set(uids), "(UID FLAGS)"), "FETCH FLAGS")
    flags: dict[int, set[str]] = {}
    for prefix, _ in fetch_rows(data):
        uid = re.search(rb"UID (\d+)", prefix)
        found = re.search(rb"FLAGS \(([^)]*)\)", prefix)
        if uid:
            flags[int(uid.group(1))] = set(found.group(1).decode().split()) if found else set()
    return flags


def connect(provider: Provider, host: str, port: int) -> imaplib.IMAP4:
    if provider.tls:
        return CountingIMAP4SSL(host, port, timeout=60)
    return CountingIMAP4(host, port, timeout=60)


def read_password(args: argparse.Namespace) -> str:
    if args.password_file:
        return Path(args.password_file).read_text().strip()
    return getpass.getpass(f"Password for {args.user} (for Gmail, an app password): ")


def run_session(imap: imaplib.IMAP4, args: argparse.Namespace, results: Results) -> None:
    folder = args.folder
    since = date.today() - timedelta(days=args.days)

    typ, data = imap.capability()
    if typ == "OK" and data and data[-1]:
        imap.capabilities = tuple(data[-1].decode().upper().split())
    if "IDLE" in imap.capabilities:
        results.record("idle-capability", "PASS", "the server supports IDLE")
    else:
        results.record("idle-capability", "FAIL", "the server does not advertise IDLE")

    # Counters only. STATUS downloads no message data.
    print(f"\n4. Mailbox counters and the {args.days}-day window (read-only)")
    status = check_ok(imap.status(folder, "(MESSAGES UNSEEN UIDVALIDITY UIDNEXT)"), "STATUS")
    counters = dict(re.findall(rb"(MESSAGES|UNSEEN|UIDVALIDITY|UIDNEXT) (\d+)", status[0]))
    total = int(counters.get(b"MESSAGES", 0))
    print(f"  {folder}: {total} messages, {int(counters.get(b'UNSEEN', 0))} unread")

    before = imap.received
    check_ok(imap.select(folder, readonly=True), "EXAMINE")
    found = check_ok(imap.uid("SEARCH", "SINCE", imap_date(since)), "UID SEARCH")
    window = [int(uid) for uid in (found[0] or b"").split()]
    results.record("window-search", "PASS", f"{len(window)} of {total} messages since {since.isoformat()}")
    if not window:
        results.record("peek", "SKIP", f"no mail in the last {args.days} days; use --days")
        results.record("seen-flag", "SKIP", "no message to test")
        run_idle(imap, args, results)
        return

    # Sizes only, for the whole window, to compare with what the probe reads.
    sizes = check_ok(imap.uid("FETCH", compact_set(window), "(UID RFC822.SIZE)"), "FETCH RFC822.SIZE")
    window_bytes = sum(int(m.group(1)) for prefix, _ in fetch_rows(sizes) if (m := re.search(rb"RFC822\.SIZE (\d+)", prefix)))

    sample = sorted(window)[-args.sample:]
    flags_before = fetch_flags(imap, sample)
    headers = check_ok(
        imap.uid("FETCH", compact_set(sample), "(UID FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)])"),
        "FETCH headers",
    )
    flags_after = fetch_flags(imap, sample)
    read_bytes = imap.received - before

    parser = email.parser.BytesHeaderParser(policy=email.policy.default)
    subjects: dict[int, str] = {}
    print(f"  Newest {len(sample)} messages (two header fields each):")
    for prefix, literal in fetch_rows(headers):
        uid = int(re.search(rb"UID (\d+)", prefix).group(1))
        message = parser.parsebytes(literal or b"")
        subject = str(message.get("Subject", "")).replace("\n", " ")
        subjects[uid] = subject
        unread = "unread" if "\\Seen" not in flags_after.get(uid, set()) else "read"
        print(f"    uid {uid:<7} {unread:<6} {str(message.get('Date', ''))[:31]:<31}  {subject[:50]}")

    if flags_before == flags_after:
        results.record("peek", "PASS", "header reads did not change any read state")
    else:
        results.record("peek", "FAIL", "a header read changed the flags of a message")

    print(
        f"\n  Download: the probe received {human_size(read_bytes)} for this step."
        f"\n  The full {len(window)} messages in the window are {human_size(window_bytes)}."
        f"\n  The service will download a full message only when you open it or its attachments."
    )

    run_idle(imap, args, results)
    run_seen_test(imap, args, results, sample[-1], subjects.get(sample[-1], ""))


def run_idle(imap: imaplib.IMAP4, args: argparse.Namespace, results: Results) -> None:
    print(f"\n5. IDLE for {args.idle} s (send yourself a message now to see a new-mail event)")
    if "IDLE" not in imap.capabilities:
        results.record("idle", "SKIP", "the server does not support IDLE")
        return
    events: list[str] = []
    started = time.monotonic()
    try:
        with imap.idle(duration=args.idle) as idler:
            for typ, datum in idler:
                events.append(f"{typ} {datum!r}")
                print(f"  event: {typ} {datum!r}")
    except imaplib.IMAP4.error as error:
        results.record("idle", "FAIL", str(error))
        return
    elapsed = time.monotonic() - started
    new_mail = sum(1 for event in events if event.startswith("EXISTS"))
    results.record("idle", "PASS", f"held IDLE for {elapsed:.0f} s, {len(events)} events, {new_mail} new-mail events")


def run_seen_test(imap: imaplib.IMAP4, args: argparse.Namespace, results: Results, uid: int, subject: str) -> None:
    print("\n6. Read-state test")
    if args.no_seen_test:
        results.record("seen-flag", "SKIP", "turned off with --no-seen-test")
        return
    if not args.yes:
        if not sys.stdin.isatty():
            results.record("seen-flag", "SKIP", "no terminal to confirm; use --yes")
            return
        answer = input(f"  Change the read state of uid {uid} ({subject[:50]!r}) and set it back? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            results.record("seen-flag", "SKIP", "declined")
            return

    check_ok(imap.select(args.folder), "SELECT")
    initial = "\\Seen" in fetch_flags(imap, [uid]).get(uid, set())
    first, second = ("-FLAGS.SILENT", "+FLAGS.SILENT") if initial else ("+FLAGS.SILENT", "-FLAGS.SILENT")
    try:
        check_ok(imap.uid("STORE", str(uid), first, "(\\Seen)"), f"STORE {first}")
        changed = "\\Seen" in fetch_flags(imap, [uid]).get(uid, set())
        if changed == initial:
            results.record("seen-flag", "FAIL", f"STORE {first} did not change the read state")
            return
    finally:
        check_ok(imap.uid("STORE", str(uid), second, "(\\Seen)"), f"STORE {second}")
    final = "\\Seen" in fetch_flags(imap, [uid]).get(uid, set())
    if final != initial:
        results.record("seen-flag", "FAIL", f"could not set uid {uid} back to {'read' if initial else 'unread'}")
        return
    state = "read" if initial else "unread"
    results.record("seen-flag", "PASS", f"uid {uid} changed and went back to {state}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Check direct IMAP access for hermes-mail. Changes nothing except the optional read-state test.")
    result.add_argument("provider", choices=sorted(PROVIDERS))
    result.add_argument("--user", required=True, help="the mail address to sign in with")
    result.add_argument("--auth", choices=["oauth", "password"], default="oauth")
    result.add_argument("--password-file", help="read the password from this file, not from a prompt")
    result.add_argument("--host", help="override the IMAP host")
    result.add_argument("--port", type=int, help="override the IMAP port")
    result.add_argument("--folder", default="INBOX")
    result.add_argument("--days", type=int, default=7, help="the sync window in days (default 7)")
    result.add_argument("--sample", type=int, default=5, help="how many recent headers to read (default 5)")
    result.add_argument("--idle", type=int, default=30, help="IDLE duration in seconds (default 30)")
    result.add_argument("--no-pkce", dest="pkce", action="store_false", help="send no PKCE challenge, as Thunderbird does")
    result.add_argument("--no-seen-test", action="store_true", help="skip the read-state test")
    result.add_argument("--yes", action="store_true", help="run the read-state test without a prompt")
    return result


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 14):
        print("This probe needs Python 3.14 or later for IMAP IDLE.", file=sys.stderr)
        print("Run it with: nix shell nixpkgs#python314 --command python3 scripts/probe.py ...", file=sys.stderr)
        return 2

    args = parser().parse_args(argv)
    provider = PROVIDERS[args.provider]
    host, port = args.host or provider.host, args.port or provider.port
    required = (OAUTH_CHECKS if args.auth == "oauth" else []) + SESSION_CHECKS
    results = Results(required)
    print(f"Probe: {args.provider} account {args.user} on {host}:{port}, {args.auth} sign-in")

    try:
        if args.auth == "oauth":
            secret = oauth_sign_in(provider, args, results)
        else:
            secret = read_password(args)
    except ProbeError as error:
        print(f"\nSign-in failed: {error}", file=sys.stderr)
        return results.summary()

    print(f"\n3. IMAP sign-in to {host}:{port}")
    try:
        imap = connect(provider, host, port)
    except OSError as error:
        results.record("imap-login", "FAIL", f"cannot connect: {error}")
        return results.summary()
    try:
        try:
            if args.auth == "oauth":
                imap.authenticate("XOAUTH2", xoauth2(args.user, secret))
            else:
                imap.login(args.user, secret)
        except imaplib.IMAP4.error as error:
            results.record("imap-login", "FAIL", auth_failure_detail(imap, error))
            return results.summary()
        results.record("imap-login", "PASS", "XOAUTH2 accepted" if args.auth == "oauth" else "password accepted")
        try:
            run_session(imap, args, results)
        except (ProbeError, imaplib.IMAP4.error, OSError) as error:
            print(f"\nThe IMAP session stopped: {error}", file=sys.stderr)
            for name in SESSION_CHECKS:
                if name not in results.rows:
                    results.record(name, "FAIL", f"did not run: {error}")
                    break
        print(f"\nTotal data from the IMAP server: {human_size(imap.received)}")
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    return results.summary()


if __name__ == "__main__":
    sys.exit(main())
