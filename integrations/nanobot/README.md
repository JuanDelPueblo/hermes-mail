# uni-mail and Nanobot

uni-mail emits an event when mail arrives. This directory turns that event into
a [Nanobot](https://github.com/HKUDS/nanobot) trigger, so that your agent wakes
up, reads the mail through the uni-mail MCP server and acts on it according to
*its own* instructions.

The bridge is a courier. It does not read, classify or judge mail, and the
message it sends holds only identifiers (account, mail ID, event ID), never
subject, sender or body. What to do with a mail is decided in Nanobot.

```
uni-maild -> uni-mail-dispatch -> uni_mail_nanobot_trigger.py -> nanobot trigger
                                                                      |
              uni-mail MCP server  <-- agent reads the mail <-- nanobot gateway
```

Verified against nanobot-ai 0.3.5. Nothing here is a dependency of uni-mail.

## 1. Give Nanobot the mail tools (MCP)

Add uni-mail to `tools.mcpServers` in the Nanobot config (`~/.nanobot/config.json`):

```json
{
  "tools": {
    "mcpServers": {
      "uni-mail": {
        "command": "/opt/uni-mail/bin/uni-mail-mcp",
        "args": [],
        "toolTimeout": 60
      }
    }
  }
}
```

`uni-mail-mcp` talks to the daemon over `/run/uni-mail/mail.sock`, which is
group-restricted. The user that runs `nanobot gateway` must be in the
`uni-mail` group (`sudo usermod -aG uni-mail NANOBOT_USER`, then restart the
gateway so it picks the group up). Use `enabledTools` if the agent should only
get some of the tools.

## 2. Create a trigger in the chat that should receive the results

In the Nanobot chat (the session where you want the agent to work on new
mail), send:

```
/trigger mail
```

Nanobot replies with an ID like `trg_8K4P2Q9X`. Note it. A trigger is bound to
that chat session, so the agent's replies, such as a summary or a question for
you, go there. Triggers live in `<workspace>/triggers/triggers.json`; the
running `nanobot gateway` consumes the queued deliveries from
`<workspace>/triggers/inbox/`.

Tell the agent in that chat (or in its standing instructions) what you want
done with a new mail. The trigger message only says *that* mail arrived.

## 3. Local setup: the command sink (recommended)

`uni_mail_nanobot_trigger.py` is a uni-mail command sink: the dispatcher starts
it, writes the event to its stdin and reads the exit status. It runs
`nanobot trigger <id> "<message>"`. Python 3, standard library only.

In `/etc/uni-mail/config.json`:

```json
{
  "dispatch": {
    "destinations": {
      "nanobot": {
        "type": "command",
        "argv": [
          "/usr/bin/python3",
          "/opt/uni-mail/integrations/nanobot/uni_mail_nanobot_trigger.py",
          "--trigger-id", "trg_XXXX",
          "--nanobot", "/home/USER/.local/bin/nanobot"
        ],
        "events": ["mail.new"],
        "timeout_seconds": 60,
        "permanent_exit_codes": [64, 65]
      }
    }
  }
}
```

Use an absolute `--nanobot` path: the dispatcher passes only a minimal
environment (`PATH`, `HOME`, `LANG`). Add `--workspace DIR` and `--config FILE`
if the gateway does not use the defaults; `nanobot trigger` must use the same
workspace as the gateway, or the delivery lands where nobody reads it.

Options: `--trigger-id` (or env `UNI_MAIL_NANOBOT_TRIGGER_ID`), `--nanobot`
(or env `NANOBOT_BIN`, default `nanobot` from `PATH`), `--workspace`,
`--config`, `--events` (comma list, default `mail.new`; add `mail.auth,mail.error`
to be told about sign-in and sync problems), `--timeout` (seconds, default 60).

Exit status:

| code | meaning | dispatcher |
| --- | --- | --- |
| 0 | queued, or event type not in `--events` (a note "ignored" on stderr) | delivered |
| 64 | bad usage: no or invalid trigger ID, bad option | dead letter |
| 65 | malformed event | dead letter |
| 75 | nanobot missing, failed or timed out (its output tail is on stderr) | retried with backoff |

An unknown or disabled trigger makes `nanobot trigger` exit 1, which becomes 75
and is retried. Fix the ID or re-enable the trigger and the retry succeeds.

### The dispatcher must run as the Nanobot user

`nanobot trigger` writes into the Nanobot workspace (`triggers/inbox/`, plus
the trigger store and its lock). The dispatcher therefore has to run as a user
that can write that workspace, normally the one that runs the gateway. It must
also reach the daemon's event socket, so add the `uni-mail` group. A drop-in:

```ini
# systemctl edit uni-mail-dispatch.service
[Service]
User=NANOBOT_USER
Group=NANOBOT_USER
SupplementaryGroups=uni-mail
# If the unit uses ProtectHome=true or ProtectSystem=strict, allow the workspace:
ProtectHome=read-only
ReadWritePaths=/home/NANOBOT_USER/.nanobot
```

The unit's `StateDirectory=uni-mail-dispatch` gives the dispatcher's own
state (`/var/lib/uni-mail-dispatch`: cursor, queue, dead letters) to the new
user at the next start. Run the `uni-mail dispatch` commands as that user too,
for example `sudo -u NANOBOT_USER /opt/uni-mail/bin/uni-mail dispatch status`.
If you do not want the dispatcher to run as that user, use the webhook receiver
below instead.

Then test the whole path with a synthetic event:

```
sudo -u NANOBOT_USER /opt/uni-mail/bin/uni-mail dispatch test nanobot
```

The agent receives a message starting with `[test]`, and a file appears in
`<workspace>/triggers/inbox/` until the gateway consumes it.

## 4. Remote or separately hosted: the webhook receiver (optional)

`uni_mail_nanobot_webhook.py` is a small HTTP server that checks the uni-mail
signature, drops repeats and then calls `nanobot trigger` itself. Use it when
the dispatcher runs on another host or as another user. It also needs to run
as a user who can write the Nanobot workspace.

```
UNI_MAIL_WEBHOOK_SECRET=... ./uni_mail_nanobot_webhook.py --trigger-id trg_XXXX \
    --nanobot /home/USER/.local/bin/nanobot --state /var/lib/uni-mail-nanobot/webhook.sqlite3
```

It refuses to start without a secret (`--secret-file` or
`UNI_MAIL_WEBHOOK_SECRET`). Defaults: `--host 127.0.0.1 --port 8790
--path /uni-mail`, body limit 256 KiB, `--timeout 10` (keep it below the destination's `timeout_seconds`, 15 by default), `--events mail.new`,
`--state ./uni-mail-nanobot-webhook.sqlite3`. Run it with the example unit
`uni-mail-nanobot-webhook.service` (it hands the secret in as a systemd
credential and hardens the service).

The matching destination in `/etc/uni-mail/config.json`:

```json
{
  "dispatch": {
    "destinations": {
      "nanobot": {
        "type": "webhook",
        "url": "http://127.0.0.1:8790/uni-mail",
        "secret_file": "/etc/uni-mail/secrets/nanobot-webhook",
        "allow_private_network": true,
        "events": ["mail.new"]
      }
    }
  }
}
```

`allow_private_network` is needed because the target is a private address.
Plain `http` is accepted for loopback; for any other host either terminate TLS
with a reverse proxy and use `https`, or set `"allow_http": true` on a network
you trust. Put the secret in the file with `chmod 600`, one line.

What the receiver checks (the uni-mail signature, v1): the body is HMAC-SHA256
signed with the secret (whitespace stripped), over
`timestamp + "." + delivery_id + "." + body`, sent as
`X-Uni-Mail-Signature: v1=<hex>`. A header with several comma-separated `v1=`
values is accepted if any matches (secret rotation: roll the receiver first).
Comparison is constant time and the timestamp must be within 300 s.

| status | when | sender |
| --- | --- | --- |
| 200 | triggered, duplicate (`{"status":"duplicate"}`) or event type ignored | done |
| 400 | malformed event | terminal |
| 401 | bad signature or stale/missing timestamp | retried (rotation) |
| 404 / 405 | wrong path / not POST | terminal |
| 409 | the same event is being handled right now | retried |
| 413 | body over 256 KiB | terminal |
| 503 | nanobot failed or timed out | retried |

Logs go to stderr (journald) and never contain the secret.

## 5. Delivery semantics

- **At-least-once.** uni-mail retries until it sees an acknowledgement, so the
  same `event_id` can arrive more than once, for example when the sink acted
  but its exit status was lost to a timeout.
- **Dedupe on `event_id`.** The receiver records each event ID in SQLite
  (`running` before nanobot is called, `done` after). A repeat of a done event
  gets `200 duplicate` without a second trigger. A failure removes the row so
  the retry runs again.
- **The duplicate window.** If the receiver is killed after `nanobot trigger`
  succeeded but before `done` is written, the row stays `running`; once it is
  older than 10 minutes the retry triggers again. That is one duplicate, never a
  loss. The command sink has no state, so it can produce a duplicate whenever
  its exit status is lost; it does not dedupe.
- **Nanobot's queue is at-least-once too.** Deliveries wait as files in
  `triggers/inbox/` until a running gateway takes them, and one interrupted
  while processing is recovered and delivered again (after repeated failures
  it moves to `triggers/failed/`). The bridge adds nothing to that, so the
  agent can see the same message twice.
- **So the agent should be idempotent.** The message carries the event ID and
  the mail ID. Have the agent check whether it already handled that mail ID
  (for example by looking at its own notes or at the mail's current state)
  before acting again. Handling a mail twice should be harmless.
- If the gateway is not running, deliveries accumulate in the inbox and are
  processed when it starts.

## 6. What the bridge does not do

It does not fetch mail, filter by sender or subject, decide urgency, or reply.
Event types are filtered only by `--events`. All judgement, including
what counts as important, what to summarise and what to ignore, belongs to the
agent's own instructions in Nanobot. The event metadata (sender, subject) is
deliberately left out of the trigger message so that a hostile mail cannot
inject text into the prompt through the bridge; the agent reads the mail
through MCP and should treat its content as untrusted.

## Tests

```
python3 -W error::ResourceWarning -m unittest tests.test_nanobot -v
UNI_MAIL_TEST_NANOBOT=/path/to/nanobot python3 -m unittest tests.test_nanobot.RealNanobotTest -v
```

The second command runs the sink against a real nanobot in a throwaway HOME
and workspace and is skipped otherwise.
