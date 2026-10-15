"""Command destinations: run a configured program for each event.

The program gets the event JSON on stdin and a small environment. Exit code
0 acknowledges the event, a code in permanent_exit_codes stops the delivery,
and any other code or a timeout leads to a retry. The program and its
arguments come only from the config file; nothing from an email reaches the
command line.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

from .config import Destination
from .sinks import SECRET_NAME, Result, redact

BASE_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
KILL_GRACE = 5


class CommandSink:
    def __init__(self, destination: Destination, secrets: list[str] | None = None):
        self.destination = destination
        # Values that must not show in the stored output: the configured
        # secret-looking variables and the webhook secrets.
        self.secrets = [value for key, value in destination.env.items() if SECRET_NAME.search(key)] + list(secrets or [])

    def environment(self, event: dict[str, Any], delivery_id: str, attempt: int) -> dict[str, str]:
        env = {key: os.environ[key] for key in BASE_ENV if key in os.environ}
        env.setdefault("PATH", DEFAULT_PATH)
        home = os.environ.get("HOME")
        if home:
            env["HOME"] = home
        env.update(self.destination.env)
        env.update({
            "UNI_MAIL_EVENT_ID": event["event_id"],
            "UNI_MAIL_EVENT_TYPE": event["event_type"],
            "UNI_MAIL_DELIVERY_ID": delivery_id,
            "UNI_MAIL_ATTEMPT": str(attempt),
            "UNI_MAIL_DESTINATION": self.destination.name,
        })
        return env

    def deliver(self, event: dict[str, Any], body: bytes, delivery_id: str, attempt: int) -> Result:
        destination = self.destination
        try:
            process = subprocess.Popen(
                list(destination.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.environment(event, delivery_id, attempt), cwd=destination.working_directory or None,
                start_new_session=True, close_fds=True,
            )
        except OSError as error:
            return Result("retry", error=f"cannot run {destination.argv[0]}: {error.strerror or error}")
        timed_out = False
        try:
            stdout, stderr = process.communicate(body, timeout=destination.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, stderr = self._stop(process)
        output = self._output(stdout, stderr)
        if timed_out:
            return Result("retry", error=f"timed out after {destination.timeout_seconds:g} s", output=output)
        code = process.returncode
        if code == 0:
            return Result("delivered", code=0, output=output)
        reason = f"killed by signal {-code}" if code < 0 else f"exit code {code}"
        if code in destination.permanent_exit_codes:
            return Result("dead", code=code, error=f"{reason} (permanent)", output=output)
        return Result("retry", code=code, error=reason, output=output)

    @staticmethod
    def _stop(process: subprocess.Popen) -> tuple[bytes, bytes]:
        """Stop the program and everything that it started."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                return process.communicate(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                continue
        return process.communicate()

    def _output(self, stdout: bytes, stderr: bytes) -> str:
        parts = []
        for name, data in (("stdout", stdout), ("stderr", stderr)):
            text = (data or b"")[-16384:].decode("utf-8", "replace").strip()
            if text:
                parts.append(f"[{name}] {text}")
        return redact("\n".join(parts), self.secrets)
