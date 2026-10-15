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
import tempfile
from typing import IO, Any

from .config import Destination
from .sinks import SECRET_NAME, Result, redact

BASE_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
KILL_GRACE = 5
# Read this much of the end of each output; the stored text is shorter (sinks.OUTPUT_LIMIT).
TAIL_BYTES = 65536


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
        """Run the program. stdin, stdout and stderr are temporary files, not
        pipes: the result is the exit of the program itself, so a background
        child that keeps a pipe open can neither hold up nor fail a delivery."""
        destination = self.destination
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            stdin.write(body)
            stdin.seek(0)
            try:
                process = subprocess.Popen(
                    list(destination.argv), stdin=stdin, stdout=stdout, stderr=stderr,
                    env=self.environment(event, delivery_id, attempt), cwd=destination.working_directory or None,
                    start_new_session=True, close_fds=True,
                )
            except OSError as error:
                return Result("retry", error=f"cannot run {destination.argv[0]}: {error.strerror or error}")
            try:
                code = process.wait(timeout=destination.timeout_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                code = self._stop(process)
            output = self._output(stdout, stderr)
        if timed_out:
            return Result("retry", error=f"timed out after {destination.timeout_seconds:g} s", output=output)
        if code == 0:
            return Result("delivered", code=0, output=output)
        reason = f"killed by signal {-code}" if code < 0 else f"exit code {code}"
        if code in destination.permanent_exit_codes:
            return Result("dead", code=code, error=f"{reason} (permanent)", output=output)
        return Result("retry", code=code, error=reason, output=output)

    @staticmethod
    def _stop(process: subprocess.Popen) -> int | None:
        """Stop the program and everything in its process group."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                return process.wait(timeout=KILL_GRACE)
            except subprocess.TimeoutExpired:
                continue
        return None

    def _output(self, stdout: IO[bytes], stderr: IO[bytes]) -> str:
        parts = []
        for name, handle in (("stdout", stdout), ("stderr", stderr)):
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace").strip()
            if text:
                parts.append(f"[{name}] {text}")
        # Redact before cutting, so that a cut cannot leave half a secret.
        return redact("\n".join(parts), self.secrets)
