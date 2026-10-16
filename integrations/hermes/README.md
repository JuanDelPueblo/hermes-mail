# Hermes Agent

This connects uni-mail to [Hermes Agent](https://github.com/NousResearch/hermes-agent)
without any Hermes code in uni-mail:

- **Tools.** Hermes starts `uni-mail-mcp` as a stdio MCP server and gets the
  mail tools (`mail_list`, `mail_show`, `mail_mark_read`, ...).
- **New mail.** A uni-mail webhook destination POSTs each `mail.new` event to
  a route of the Hermes webhook platform. The route starts an agent run with a
  prompt that holds only the account and the mail ID; the agent reads the
  message through MCP, decides, and its answer goes to a chat. An answer of
  `[SILENT]` sends nothing.

uni-mail decides nothing: what counts as important, what to write and when to
mark mail as read is in the route's prompt, in Hermes.

The example assumes uni-mail and Hermes run as user services of the same user
(`deploy/user/`, docs/INSTALL.md), so the socket needs no shared group. With
the system service, the user of Hermes must be in the `uni-mail` group, and
`--socket` is `/run/uni-mail/mail.sock`.

## 1. The shared secret

```sh
install -d -m 0700 ~/.config/uni-mail/secrets
openssl rand -hex 32 > ~/.config/uni-mail/secrets/hermes
chmod 0600 ~/.config/uni-mail/secrets/hermes
echo "UNI_MAIL_WEBHOOK_SECRET=$(cat ~/.config/uni-mail/secrets/hermes)" >> ~/.hermes/.env
```

## 2. The uni-mail destination

In the `dispatch` section of `~/.config/uni-mail/config.json`:

```json
"destinations": {
  "hermes": {
    "type": "webhook",
    "url": "http://127.0.0.1:8644/webhooks/uni-mail",
    "secret_file": "/home/USER/.config/uni-mail/secrets/hermes",
    "allow_private_network": true,
    "events": ["mail.new"],
    "accounts": ["university"]
  }
}
```

- Plain HTTP is allowed because the address is loopback; loopback still needs
  `allow_private_network`.
- `accounts` limits the events to the accounts that Hermes should see.
- Each request carries a [Standard Webhooks](https://www.standardwebhooks.com/)
  signature (`webhook-id`, `webhook-timestamp`, `webhook-signature`), which the
  Hermes webhook platform checks. `webhook-id` is the uni-mail delivery ID,
  the same for every retry, so Hermes drops a retry of a delivery it already
  started (for one hour).
- Hermes answers `202` when it accepts the event, before the agent runs. The
  dispatcher marks the delivery done then; a failed agent run shows in the
  Hermes log (`~/.hermes/logs/`), not in `uni-mail dispatch`.

## 3. The Hermes config

Merge `hermes-config.example.yaml` into `~/.hermes/config.yaml`: the
`uni-mail` MCP server and the `uni-mail` webhook route. Write your rules into
the route's prompt and set the chat of `deliver`/`deliver_extra`. Then restart
the gateway (this interrupts running chats):

```sh
systemctl --user restart hermes-gateway.service
```

- `toolsets: ["mcp-uni-mail"]` gives the runs of this route only the mail
  tools. The mail is text from anyone; keep shells and other tools out of
  these runs.
- `trust: full` is needed for unattended runs: on an `untrusted` server Hermes
  asks for approval before each write call, such as `mail_mark_read`.
- Hermes names MCP tools after the server; instructions or skills that name
  tools of the old hermes-mail plugin need the new names.

## 4. Check it

```sh
uni-mail dispatch check
uni-mail dispatch test hermes      # a synthetic event; expect "delivered" (HTTP 202)
uni-mail dispatch status
```

The test event has the mail ID `test.0000000000000000`, which does not exist:
the agent run fails to read it, which is expected. To try the whole path with
real mail, set the route's `deliver` to `log` first, then replay a recent
event and read the Hermes log:

```sh
uni-mail dispatch replay hermes --since 1d
```
