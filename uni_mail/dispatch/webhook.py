"""Webhook destinations: a signed HTTP POST of the event.

Signature, version 1 (docs/EVENTS.md):

    X-Uni-Mail-Timestamp: <unix seconds>
    X-Uni-Mail-Delivery-Id: <delivery ID, the same for each retry>
    X-Uni-Mail-Signature: v1=<hex HMAC-SHA256(secret, timestamp + "." + delivery_id + "." + body)>

Each request also carries a Standard Webhooks signature
(https://www.standardwebhooks.com/), which receivers such as Svix
libraries and the Hermes webhook platform check without uni-mail code:

    webhook-id: <delivery ID>
    webhook-timestamp: <unix seconds>
    webhook-signature: v1,<base64 HMAC-SHA256(key, delivery_id + "." + timestamp + "." + body)>

The secret is the text of the secret file or variable without the
surrounding white space. For the Standard Webhooks signature, a secret of
the form whsec_<base64> gives the decoded bytes as the key, as the
specification says; any other secret is used as UTF-8 text. A receiver checks the signature in constant time,
refuses a timestamp more than five minutes away and drops an event ID that
it has seen.

The request goes to the address that was checked: the host name is resolved
once, every address must be allowed, and the connection goes to that address
(no second lookup, no redirects).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse
from typing import Any

from .. import __version__
from ..config import ConfigError
from .config import Destination, check_url
from .sinks import Result

RESPONSE_LIMIT = 4096
TOLERANCE = 300
# Answers that a later attempt may change. 401 and 403 are here because a
# secret rotation makes them for a while.
RETRY_STATUS = {401, 403, 408, 409, 425, 429}
# 100.64.0.0/10 (carrier-grade NAT) counts as private, though Python's ipaddress does not say so.
SHARED = ipaddress.ip_network("100.64.0.0/10")


def signature(secret: str, timestamp: str, delivery_id: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + delivery_id.encode() + b"." + body
    return "v1=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def standard_key(secret: str) -> bytes:
    """The HMAC key of a Standard Webhooks signature: the decoded bytes of a whsec_ secret, else the text."""
    if secret.startswith("whsec_"):
        try:
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        except (binascii.Error, ValueError):
            raise ConfigError("the secret starts with whsec_ but the rest is not base64") from None
        if not key:
            raise ConfigError("the whsec_ secret is empty")
        return key
    return secret.encode()


def standard_signature(secret: str, message_id: str, timestamp: str, body: bytes) -> str:
    message = message_id.encode() + b"." + timestamp.encode() + b"." + body
    return "v1," + base64.b64encode(hmac.new(standard_key(secret), message, hashlib.sha256).digest()).decode()


def verify(secret: str, headers: dict[str, str], body: bytes, now: float | None = None, tolerance: int = TOLERANCE) -> bool:
    """Check a received webhook. `headers` must have lower-case names."""
    timestamp = headers.get("x-uni-mail-timestamp", "")
    delivery_id = headers.get("x-uni-mail-delivery-id", "")
    if not (timestamp.isascii() and timestamp.isdigit()) or not delivery_id:
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > tolerance:
        return False
    expected = signature(secret, timestamp, delivery_id, body)
    received = [value.strip() for value in headers.get("x-uni-mail-signature", "").split(",")]
    return any(hmac.compare_digest(expected, value) for value in received if value)


def address_problem(address: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_private: bool) -> str:
    """Why a webhook may not go to this address, or "" when it may."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_link_local:
        return "a link-local address (such as a cloud metadata service)"
    if address.is_multicast or address.is_unspecified:
        return "a multicast or unspecified address"
    # Before the reserved check: Python counts ::1 as reserved.
    if address.is_loopback or address.is_private or address in SHARED:
        return "" if allow_private else "a private or loopback address; set allow_private_network to allow it"
    if address.is_reserved:
        return "a reserved address"
    if not address.is_global:
        return "an address that is not global"
    return ""


def resolve(host: str, port: int, allow_private: bool) -> list[str]:
    """Resolve the host and return its addresses. Refuse the host when any address is not allowed."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise OSError(f"cannot resolve {host}: {error.strerror}") from None
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    for value in addresses:
        problem = address_problem(ipaddress.ip_address(value.split("%")[0]), allow_private)
        if problem:
            raise ConfigError(f"{host} resolves to {value}, {problem}")
    if not addresses:
        raise OSError(f"{host} has no address")
    return addresses


def _connect(addresses: list[str], port: int, timeout: float) -> socket.socket:
    """Connect to the first checked address that answers, in the resolver's order."""
    error: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout)
        except OSError as failure:
            error = failure
    raise error or OSError("no address")


class _Pinned(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, addresses: list[str], timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.addresses = addresses

    def connect(self) -> None:
        self.sock = _connect(self.addresses, self.port, self.timeout)


class _PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, addresses: list[str], timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.addresses = addresses

    def connect(self) -> None:
        raw = _connect(self.addresses, self.port, self.timeout)
        # The certificate must match the host name, not the address.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _retry_after(value: str | None) -> float | None:
    if value and value.strip().isdigit():
        return float(value.strip())
    return None


class WebhookSink:
    def __init__(self, destination: Destination):
        self.destination = destination
        self.parts = check_url(destination.url, destination.allow_http)

    def secrets(self) -> list[str]:
        try:
            value = self.destination.secret()
        except ConfigError:
            return []
        return [value] if value else []

    def deliver(self, event: dict[str, Any], body: bytes, delivery_id: str, attempt: int) -> Result:
        destination = self.destination
        try:
            secret = destination.secret()
        except ConfigError as error:
            return Result("retry", error=str(error))
        if not secret:
            return Result("retry", error=f"no secret: {destination.secret_env} is empty or not set")
        parts = self.parts
        tls = parts.scheme == "https"
        port = parts.port or (443 if tls else 80)
        try:
            addresses = resolve(parts.hostname or "", port, destination.allow_private_network)
        except ConfigError as error:
            return Result("dead", error=f"refused: {error}")
        except OSError as error:
            return Result("retry", error=str(error))
        timestamp = str(int(time.time()))
        try:
            standard = standard_signature(secret, delivery_id, timestamp, body)
        except ConfigError as error:
            return Result("retry", error=f"destination {destination.name}: {error}")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"uni-mail/{__version__}",
            "X-Uni-Mail-Event-Type": event["event_type"],
            "X-Uni-Mail-Event-Id": event["event_id"],
            "X-Uni-Mail-Delivery-Id": delivery_id,
            "X-Uni-Mail-Attempt": str(attempt),
            "X-Uni-Mail-Timestamp": timestamp,
            "X-Uni-Mail-Signature": signature(secret, timestamp, delivery_id, body),
            "webhook-id": delivery_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": standard,
        }
        target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection = (_PinnedTLS if tls else _Pinned)(parts.hostname or "", port, addresses, destination.timeout_seconds)
        try:
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            text = response.read(RESPONSE_LIMIT).decode("utf-8", "replace")
            status = response.status
            retry_after = _retry_after(response.getheader("Retry-After"))
        except (OSError, http.client.HTTPException) as error:
            return Result("retry", error=f"{type(error).__name__}: {error}" if str(error) else type(error).__name__)
        finally:
            connection.close()
        if 200 <= status < 300:
            return Result("delivered", code=status, output=text)
        detail = f"HTTP {status}"
        if status in (401, 403):
            detail += " (the receiver refused the signature; check the shared secret)"
        if 300 <= status < 400:
            return Result("dead", code=status, error=f"{detail}: redirects are not followed; set the final URL", output=text)
        if status in RETRY_STATUS or status >= 500:
            return Result("retry", code=status, error=detail, output=text, retry_after=retry_after)
        return Result("dead", code=status, error=detail, output=text)
