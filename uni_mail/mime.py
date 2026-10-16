"""Decode message parts that the service fetches one at a time."""

from __future__ import annotations

import binascii
import codecs
import email.parser
import email.policy
import mimetypes
import quopri
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from .imapproto import decode_words

HEADER_FIELDS = ("DATE", "FROM", "TO", "CC", "SUBJECT", "MESSAGE-ID")


def decode_transfer(data: bytes, encoding: str, partial: bool = False) -> bytes:
    """Decode a Content-Transfer-Encoding. With partial=True, the data is the
    start of a longer part, so the last incomplete base64 group is dropped."""
    encoding = (encoding or "7bit").lower()
    if encoding == "base64":
        compact = re.sub(rb"[^A-Za-z0-9+/=]", b"", data)
        if partial:
            compact = compact[: len(compact) - len(compact) % 4]
        try:
            return binascii.a2b_base64(compact)
        except binascii.Error:
            return binascii.a2b_base64(compact[: len(compact) - len(compact) % 4] + b"==")
    if encoding == "quoted-printable":
        if partial:
            # Drop a soft line break or an escape that the cut split.
            data = re.sub(rb"=[0-9A-Fa-f]?$", b"", data)
        return quopri.decodestring(data)
    return data


def decode_text(data: bytes, charset: str = "", partial: bool = False) -> str:
    """Decode text with the declared charset and common fallbacks. With
    partial=True, the data is the start of a longer part, so an incomplete
    character at the end is dropped."""
    encodings = [charset] if charset else []
    encodings += ["utf-8", "windows-1252", "latin-1"]
    for encoding in dict.fromkeys(encodings):
        try:
            if partial:
                return codecs.getincrementaldecoder(encoding)().decode(data, final=False)
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


class _HtmlText(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
        "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }
    SKIP_TAGS = {"head", "script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag == "li":
            self.fragments.append("\n- ")
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.fragments.append(data)

    def text(self) -> str:
        value = "".join(self.fragments).replace("\xa0", " ")
        value = re.sub(r"[ \t\f\v]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def html_to_text(value: str) -> str:
    parser = _HtmlText()
    try:
        parser.feed(value)
        parser.close()
        return parser.text()
    except Exception:
        # Broken marketing HTML must not stop the sync.
        return re.sub(r"<[^>]+>", " ", value).strip()


def choose_body(plain: str, html: str) -> str:
    """Prefer text/plain, unless it is almost empty and the HTML has the content.
    Some Exchange messages send a short plain part and the real text in HTML."""
    plain, html = plain.strip(), html.strip()
    if plain and not (len(plain) < 500 and len(html) > max(400, len(plain) * 2)):
        return plain
    return html or plain


def parse_headers(raw: bytes) -> dict[str, str]:
    message = email.parser.BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw or b"")
    return {
        "sender": decode_words(message.get("From", "")),
        "to": decode_words(message.get("To", "")),
        "cc": decode_words(message.get("Cc", "")),
        "subject": decode_words(message.get("Subject", "")).replace("\r", "").replace("\n", " "),
        "message_id": decode_words(message.get("Message-ID", "")).strip(),
        "date": header_date(message.get("Date", "")),
    }


def header_date(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(decode_words(value).strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def internal_date(value: str) -> str:
    """Parse an INTERNALDATE such as 07-Sep-2026 10:00:00 +0000."""
    try:
        return datetime.strptime(value, "%d-%b-%Y %H:%M:%S %z").isoformat()
    except (TypeError, ValueError):
        return ""


def safe_filename(name: str, index: int, content_type: str) -> str:
    base = Path(name).name if name else ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if cleaned:
        return cleaned[:180]
    return f"attachment-{index}{mimetypes.guess_extension(content_type) or ''}"
