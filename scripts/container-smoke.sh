#!/bin/sh
# Start the daemon from localhost/uni-mail:latest with the options of
# deploy/podman/uni-mail.container and an empty account list, then reach its
# socket from a second container, as the dispatcher and the MCP server do.
set -eu
work=$(mktemp -d)
trap 'podman rm -f uni-mail-smoke >/dev/null 2>&1 || true; podman volume rm -f uni-mail-smoke-state uni-mail-smoke-run uni-mail-smoke-dispatch >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
mkdir "$work/config"
printf '%s\n' '{"state_dir": "/var/lib/uni-mail", "socket": "/run/uni-mail/mail.sock", "accounts": {},
 "dispatch": {"state_dir": "/var/lib/uni-mail-dispatch"}}' > "$work/config/config.json"
lockdown="--userns keep-id:uid=10001,gid=10001 --read-only --cap-drop all --security-opt no-new-privileges --umask 0077"
# shellcheck disable=SC2086
podman run -d --name uni-mail-smoke $lockdown \
  -v "$work/config:/etc/uni-mail:ro,z" -v uni-mail-smoke-state:/var/lib/uni-mail -v uni-mail-smoke-run:/run/uni-mail \
  localhost/uni-mail:latest uni-maild --config /etc/uni-mail/config.json
for _ in $(seq 1 30); do
  podman exec uni-mail-smoke uni-mail status >/dev/null 2>&1 && break
  sleep 1
done
podman exec uni-mail-smoke uni-mail status
# shellcheck disable=SC2086
podman run --rm $lockdown -v "$work/config:/etc/uni-mail:ro,z" -v uni-mail-smoke-run:/run/uni-mail \
  -v uni-mail-smoke-dispatch:/var/lib/uni-mail-dispatch \
  localhost/uni-mail:latest uni-mail --config /etc/uni-mail/config.json dispatch once
podman exec uni-mail-smoke uni-mail-mcp --help >/dev/null
echo "container smoke test passed"
