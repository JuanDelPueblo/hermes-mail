# Run uni-mail with Podman

This runs uni-mail as rootless Podman containers, managed by systemd through
[Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html).
It is an alternative to the native install (docs/INSTALL.md): pick one, not
both, for the same accounts.

| Unit (`deploy/podman/`) | Container | What it runs |
| --- | --- | --- |
| `uni-mail.container` | `uni-mail` | `uni-maild`: IMAP, OAuth, the index and the socket |
| `uni-mail-dispatch.container` | `uni-mail-dispatch` | `uni-mail dispatch run`: webhook delivery |
| `uni-mail-mcp.container` (optional) | `uni-mail-mcp` | `uni-mail-mcp` over Streamable HTTP on `127.0.0.1:8765` |
| `uni-mail-state.volume` | | `/var/lib/uni-mail`: index, cache, exports, tokens |
| `uni-mail-dispatch.volume` | | `/var/lib/uni-mail-dispatch`: the delivery queue |
| `uni-mail-run.volume` | | `/run/uni-mail`: the socket, shared by the three containers |

The image (`Containerfile`) holds Python 3.14 with uni-mail and the MCP SDK,
and runs as the unprivileged user 10001. The units map that user to your own
user (`UserNS=keep-id:uid=10001,gid=10001`), so the config you own is
readable in the container and the files in the volumes belong to you. Every
container has a read-only root file system, no capabilities and
`no-new-privileges`.

It needs Podman 5 with Quadlet (Fedora: `sudo dnf install podman`).

## 1. Build the image

```sh
podman build -t localhost/uni-mail:latest .
```

Rebuild after an update and restart the units; the volumes keep the state.

## 2. Write the config

The containers read `~/.config/uni-mail/` read-only at `/etc/uni-mail/`.
Paths in the config are paths in the container: start from
`deploy/podman/config.example.json`, which has the right `state_dir`,
`socket` and `dispatch.state_dir`.

```sh
install -d -m 0700 ~/.config/uni-mail ~/.config/uni-mail/secrets ~/.config/uni-mail/passwords
install -m 0600 deploy/podman/config.example.json ~/.config/uni-mail/config.json
```

- A password file goes in `~/.config/uni-mail/passwords/` and is named in
  the config as `/etc/uni-mail/passwords/<name>`.
- A webhook secret goes in `~/.config/uni-mail/secrets/` and is named as
  `/etc/uni-mail/secrets/<name>`.
- The mount uses the SELinux option `z`: Podman relabels the directory once
  so that the containers may read it, and you keep full access to it.

## 3. Install and start the units

```sh
install -d ~/.config/containers/systemd
cp deploy/podman/uni-mail.container deploy/podman/uni-mail-dispatch.container \
   deploy/podman/*.volume ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start uni-mail.service
loginctl enable-linger "$USER"     # run without a login session, and start at boot
podman exec uni-mail uni-mail status
```

Quadlet units are generated, so `systemctl --user enable` does not apply:
the `[Install]` section starts them at boot. Logs:
`journalctl --user -u uni-mail -u uni-mail-dispatch`.

These units have the same names as the native user units in `deploy/user/`.
A unit in `~/.config/systemd/user/` hides a Quadlet unit with the same name,
so remove the native units before you switch.

## 4. Sign in

```sh
podman exec -it uni-mail uni-mail auth login university
```

Open the printed URL, sign in, and paste the `localhost` URL from the
address bar back into the command (docs/INSTALL.md, "Sign in"). Every other
command works the same way through `podman exec uni-mail uni-mail ...`. A
shell alias helps:

```sh
alias uni-mail='podman exec -i uni-mail uni-mail'
```

## 5. Deliver events

Add destinations to the `dispatch` section (docs/EVENTS.md), then:

```sh
podman exec uni-mail-dispatch uni-mail dispatch check
systemctl --user start uni-mail-dispatch.service
podman exec uni-mail-dispatch uni-mail dispatch test <destination>
```

- Use **webhook** destinations. A command destination runs inside the
  dispatcher container, which holds only uni-mail and Python; it cannot run a
  program of the host such as `nanobot trigger`. For Nanobot, use its webhook
  receiver (`integrations/nanobot/`) on the host.
- The dispatcher uses the host network (`Network=host`), so a webhook can
  reach a receiver on the host's loopback, such as the Hermes webhook
  platform at `http://127.0.0.1:8644` (integrations/hermes). Loopback
  addresses need `allow_private_network`. If all receivers are on other
  hosts, remove `Network=host` from the unit.

## 6. MCP

Two ways for an MCP client on the host:

- **stdio**, started by the client:

  ```json
  {"command": "podman", "args": ["exec", "-i", "uni-mail", "uni-mail-mcp"]}
  ```

  For Hermes: `mcp_servers.uni-mail.command: podman` with these `args`.

- **Streamable HTTP**, as a service with a bearer token:

  ```sh
  openssl rand -hex 32 | podman secret create uni-mail-mcp-token -
  cp deploy/podman/uni-mail-mcp.container ~/.config/containers/systemd/
  systemctl --user daemon-reload
  systemctl --user start uni-mail-mcp.service
  ```

  The server listens on `http://127.0.0.1:8765/mcp` and wants
  `Authorization: Bearer <token>`. Read the token for the client's config
  with `podman run --rm --secret uni-mail-mcp-token docker.io/library/python:3.14-slim cat /run/secrets/uni-mail-mcp-token`.
  Nanobot needs `127.0.0.1/32` in `tools.ssrfWhitelist` for a loopback URL.

## Move an existing installation into the volumes

Stop the old service first: two daemons on copies of the same state would
both sync and both create events.

From the native user service (docs/INSTALL.md, "As a user service"):

```sh
systemctl --user disable --now uni-mail-dispatch.service uni-mail.service
rm ~/.config/systemd/user/uni-mail.service ~/.config/systemd/user/uni-mail-dispatch.service
cp ~/.config/uni-mail/config.json ~/.config/uni-mail/config.native.json
# Edit ~/.config/uni-mail/config.json: state_dir /var/lib/uni-mail, socket
# /run/uni-mail/mail.sock, dispatch.state_dir /var/lib/uni-mail-dispatch,
# and /etc/uni-mail/... for password and secret files.
podman volume create uni-mail-state
podman volume create uni-mail-dispatch
podman run --rm --userns keep-id:uid=10001,gid=10001 \
  -v ~/.local/state/uni-mail:/from:ro,z -v uni-mail-state:/var/lib/uni-mail \
  localhost/uni-mail:latest sh -c 'cp -a /from/. /var/lib/uni-mail/ && chmod 0700 /var/lib/uni-mail'
podman run --rm --userns keep-id:uid=10001,gid=10001 \
  -v ~/.local/state/uni-mail-dispatch:/from:ro,z -v uni-mail-dispatch:/var/lib/uni-mail-dispatch \
  localhost/uni-mail:latest sh -c 'cp -a /from/. /var/lib/uni-mail-dispatch/ && chmod 0700 /var/lib/uni-mail-dispatch'
```

Then install and start the units as above. The accounts need no new sign-in
and the dispatcher continues where it stopped.

From hermes-mail: run `sudo uni-mail migrate --user "$USER"` from a native
install of the package first (docs/MIGRATION.md), then move that state into
the volumes as above.

## Rootful Podman

The units are written for rootless Podman under a user. For a system
service, put them in `/etc/containers/systemd/`, replace `%h/.config/uni-mail`
with `/etc/uni-mail`, remove the `UserNS=` lines (the files must then be
readable by uid 10001), and use `WantedBy=multi-user.target`.
