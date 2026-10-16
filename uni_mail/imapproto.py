"""Parse IMAP FETCH responses and BODYSTRUCTURE without the message data.

imaplib returns a FETCH response as a list. It removes "* <n> FETCH", and it
splits each literal into a (text, literal) tuple. This module turns that list
back into Python values: lists, str atoms, str quoted strings, bytes literals
and None for NIL.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import date
from email.header import decode_header
from email.utils import collapse_rfc2231_value, decode_params, unquote
from typing import Any

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_LPAREN, _RPAREN = object(), object()
_LITERAL_TAIL = re.compile(rb"\{(\d+)\+?\}$")


class ParseError(ValueError):
    """The server sent a response that this parser does not understand."""


def imap_date(value: date) -> str:
    """Return a date in the IMAP SEARCH format, such as 07-Sep-2026."""
    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year}"


def encode_mailbox(name: str) -> str:
    """Encode a mailbox name in the modified UTF-7 of RFC 3501, section 5.1.3."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            data = base64.b64encode("".join(run).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + data.replace("/", ",") + "-")
            run.clear()

    for char in name:
        if " " <= char <= "~":
            flush()
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(out)


def decode_mailbox(name: str) -> str:
    """Decode a mailbox name from the modified UTF-7 of RFC 3501. A name that is not valid stays as it is."""

    def convert(match: re.Match[str]) -> str:
        data = match.group(1)
        if not data:
            return "&"
        padded = data.replace(",", "/") + "=" * (-len(data) % 4)
        try:
            return base64.b64decode(padded, validate=True).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", convert, name)


def quote_mailbox(name: str) -> str:
    """Encode and quote a mailbox name for imaplib, which sends arguments as they are."""
    return '"' + encode_mailbox(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_LINE = re.compile(rb"^\((?P<flags>[^)]*)\)\s+(?:\"(?:\\.|[^\"\\])*\"|NIL)\s+(?P<name>.*)$", re.DOTALL)


def parse_list(data: list[Any]) -> list[dict[str, Any]]:
    """Parse the data of an imaplib LIST call into folders: {"name", "flags"}.
    The name is decoded. A folder that cannot be selected (\\Noselect or
    \\NonExistent) is left out."""
    folders: list[dict[str, Any]] = []
    index = 0
    while index < len(data):
        item = data[index]
        index += 1
        literal: bytes | None = None
        if isinstance(item, tuple):
            item, literal = item[0], item[1]
            if index < len(data) and data[index] == b"":
                index += 1
        if not isinstance(item, bytes):
            continue
        match = _LIST_LINE.match(item.strip())
        if match is None:
            raise ParseError(f"cannot read the LIST line {item!r}")
        raw = match.group("name").strip()
        if literal is not None:
            name = literal
        elif raw.startswith(b'"'):
            name = re.sub(rb"\\(.)", rb"\1", raw[1:-1])
        else:
            name = raw
        flags = match.group("flags").decode(errors="replace").split()
        if {flag.lower() for flag in flags} & {"\\noselect", "\\nonexistent"}:
            continue
        folders.append({"name": decode_mailbox(name.decode("ascii", errors="replace")), "flags": flags})
    return folders


def compact_set(uids: list[int]) -> str:
    """Return an IMAP sequence set such as 3:5,7."""
    ordered = sorted(set(uids))
    if not ordered:
        raise ValueError("empty UID set")
    parts: list[str] = []
    start = previous = ordered[0]
    for uid in ordered[1:] + [None]:
        if uid is not None and uid == previous + 1:
            previous = uid
            continue
        parts.append(str(start) if start == previous else f"{start}:{previous}")
        if uid is not None:
            start = previous = uid
    return ",".join(parts)


def chunks(values: list[int], size: int) -> list[list[int]]:
    ordered = sorted(values)
    return [ordered[index:index + size] for index in range(0, len(ordered), size)]


def _tokenize_text(text: bytes, tokens: list[Any]) -> int | None:
    """Add the tokens of one text segment. Return the literal size at its end."""
    literal = _LITERAL_TAIL.search(text)
    if literal:
        text = text[:literal.start()]
    position, length = 0, len(text)
    while position < length:
        char = text[position:position + 1]
        if char in b" \r\n\t":
            position += 1
        elif char == b"(":
            tokens.append(_LPAREN)
            position += 1
        elif char == b")":
            tokens.append(_RPAREN)
            position += 1
        elif char == b'"':
            position += 1
            value = bytearray()
            while position < length and text[position:position + 1] != b'"':
                if text[position:position + 1] == b"\\":
                    position += 1
                value += text[position:position + 1]
                position += 1
            position += 1
            tokens.append(bytes(value).decode("utf-8", errors="replace"))
        else:
            start = position
            while position < length and text[position:position + 1] not in b" ()\r\n\t":
                if text[position:position + 1] == b"[":
                    close = text.find(b"]", position)
                    position = length if close < 0 else close + 1
                else:
                    position += 1
            atom = text[start:position].decode("utf-8", errors="replace")
            tokens.append(None if atom.upper() == "NIL" else atom)
    return int(literal.group(1)) if literal else None


def tokenize(data: list[Any]) -> list[Any]:
    tokens: list[Any] = []
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            size = _tokenize_text(item[0], tokens)
            if size is None:
                raise ParseError("a literal has no size marker")
            tokens.append(bytes(item[1]))
        else:
            _tokenize_text(item, tokens)
    return tokens


def _parse(tokens: list[Any], position: int) -> tuple[Any, int]:
    token = tokens[position]
    if token is _LPAREN:
        values: list[Any] = []
        position += 1
        while position < len(tokens) and tokens[position] is not _RPAREN:
            value, position = _parse(tokens, position)
            values.append(value)
        if position >= len(tokens):
            raise ParseError("a list has no closing parenthesis")
        return values, position + 1
    if token is _RPAREN:
        raise ParseError("unexpected closing parenthesis")
    return token, position + 1


def parse_fetch(data: list[Any]) -> list[dict[str, Any]]:
    """Return one dict for each FETCH response, keyed by upper-case item name.

    A section name keeps its case, for example "BODY[1]<0>" or
    "BODY[HEADER.FIELDS (DATE)]". The server returns BODY.PEEK as BODY.
    """
    tokens = tokenize(data)
    responses: list[dict[str, Any]] = []
    position = 0
    while position < len(tokens):
        sequence, position = _parse(tokens, position)
        if position >= len(tokens):
            raise ParseError(f"FETCH response {sequence!r} has no data")
        items, position = _parse(tokens, position)
        if not isinstance(items, list) or len(items) % 2:
            raise ParseError("FETCH data is not a list of name and value pairs")
        response: dict[str, Any] = {}
        for name, value in zip(items[0::2], items[1::2], strict=True):
            head, bracket, rest = str(name).partition("[")
            response[head.upper() + bracket + rest] = value
        responses.append(response)
    return responses


def section(response: dict[str, Any], prefix: str) -> Any:
    """Return the first value whose key starts with a section prefix such as BODY[1]."""
    for key, value in response.items():
        if key.upper().startswith(prefix.upper()):
            return value
    return None


def decode_words(value: str | None) -> str:
    """Decode RFC 2047 encoded words. Keep the spacing of the plain text."""
    if not value:
        return ""
    value = re.sub(r"\r?\n[ \t]+", " ", value)
    try:
        pieces = decode_header(value)
    except (LookupError, TypeError, ValueError):
        return value
    text: list[str] = []
    for piece, charset in pieces:
        if isinstance(piece, str):
            text.append(piece)
            continue
        try:
            text.append(piece.decode(charset or "ascii", errors="replace"))
        except LookupError:
            text.append(piece.decode("utf-8", errors="replace"))
    return "".join(text)


def _params(values: Any) -> dict[str, str]:
    """Return BODYSTRUCTURE parameters with RFC 2231 and RFC 2047 decoded."""
    if not isinstance(values, list):
        return {}
    pairs = [
        (name.lower(), '"' + _text(value).replace("\\", "\\\\").replace('"', '\\"') + '"')
        for name, value in zip(values[0::2], values[1::2], strict=False)
        if isinstance(name, str) and isinstance(value, (str, bytes))
    ]
    try:
        decoded = decode_params([("", "")] + pairs)[1:]
    except (LookupError, ValueError, TypeError):
        decoded = [(name, value.strip('"')) for name, value in pairs]
    return {name: decode_words(unquote(collapse_rfc2231_value(value))) for name, value in decoded}


@dataclass
class Part:
    """One leaf of a BODYSTRUCTURE."""

    number: str
    content_type: str
    params: dict[str, str] = field(default_factory=dict)
    encoding: str = "7bit"
    size: int = 0
    content_id: str = ""
    disposition: str = ""
    disposition_params: dict[str, str] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return self.disposition_params.get("filename") or self.params.get("name") or ""

    @property
    def charset(self) -> str:
        return self.params.get("charset", "")

    @property
    def is_attachment(self) -> bool:
        if self.disposition == "attachment":
            return True
        if self.content_type == "message/rfc822":
            return True
        return bool(self.filename)

    def to_dict(self) -> dict[str, Any]:
        return {
            "part": self.number,
            "content_type": self.content_type,
            "charset": self.charset,
            "encoding": self.encoding,
            "size": self.size,
            "filename": self.filename,
            "disposition": self.disposition,
            "content_id": self.content_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Part":
        params = {"charset": value["charset"]} if value.get("charset") else {}
        disposition_params = {"filename": value["filename"]} if value.get("filename") else {}
        return cls(
            number=value["part"], content_type=value["content_type"], params=params,
            encoding=value.get("encoding", "7bit"), size=int(value.get("size", 0)),
            content_id=value.get("content_id", ""), disposition=value.get("disposition", ""),
            disposition_params=disposition_params,
        )


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _disposition(value: Any) -> tuple[str, dict[str, str]]:
    if isinstance(value, list) and value:
        return _text(value[0]).lower(), _params(value[1] if len(value) > 1 else None)
    return "", {}


def parse_bodystructure(structure: Any, number: str = "") -> list[Part]:
    """Return the leaf parts of a BODYSTRUCTURE in order. A message/rfc822
    part stays one leaf, so its inner parts never become attachments."""
    if not isinstance(structure, list) or not structure:
        raise ParseError("BODYSTRUCTURE is not a list")
    if isinstance(structure[0], list):
        parts: list[Part] = []
        index = 0
        for child in structure:
            if not isinstance(child, list):
                break
            index += 1
            prefix = f"{number}.{index}" if number else str(index)
            parts.extend(parse_bodystructure(child, prefix))
        if not parts:
            raise ParseError("multipart BODYSTRUCTURE has no parts")
        return parts

    main, sub = _text(structure[0]).lower(), _text(structure[1]).lower()
    content_type = f"{main}/{sub}"
    extension = 7
    if main == "text":
        extension = 8
    elif content_type == "message/rfc822":
        extension = 10
    disposition, disposition_params = _disposition(structure[extension + 1] if len(structure) > extension + 1 else None)
    content_id = _text(structure[3]).strip("<>") if len(structure) > 3 else ""
    return [Part(
        number=number or "1",
        content_type=content_type,
        params=_params(structure[2] if len(structure) > 2 else None),
        encoding=_text(structure[5]).lower() if len(structure) > 5 else "7bit",
        size=_int(structure[6]) if len(structure) > 6 else 0,
        content_id=content_id,
        disposition=disposition,
        disposition_params=disposition_params,
    )]


def text_parts(parts: list[Part]) -> tuple[Part | None, Part | None]:
    """Return the first text/plain and text/html parts that are not attachments."""
    plain = next((part for part in parts if part.content_type == "text/plain" and not part.is_attachment), None)
    html = next((part for part in parts if part.content_type == "text/html" and not part.is_attachment), None)
    return plain, html


def attachment_parts(parts: list[Part]) -> list[Part]:
    return [part for part in parts if part.is_attachment]
