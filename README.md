# hermes-mail

IMAP mail access for the [Hermes agent](https://github.com/NousResearch/hermes-agent),
with no desktop mail client. The repository has these parts:

- `hermes-maild`: a small service that keeps an index of recent mail and
  talks IMAP. The `hermes-mail` command is its client.
- A Hermes plugin with `mail_*` tools, a mail skill and a notifier that
  triages new mail into chat messages.
- Deployment files for systemd: units, sysusers, tmpfiles and a config
  example. See [docs/INSTALL.md](docs/INSTALL.md).
- An optional NixOS module that runs the service and connects it to Hermes.
  See [docs/NIXOS.md](docs/NIXOS.md).

The service and the plugin use only the Python standard library. The service
needs Python 3.14 or later. The plugin runs on the Python of Hermes (3.12 or
later) and bundles its own socket client, so Hermes needs no hermes-mail
package.

The design and its reasons are in [docs/PLAN.md](docs/PLAN.md).

## Accounts

| Account                      | `provider`  | Sign-in                                        |
| ---------------------------- | ----------- | ---------------------------------------------- |
| Microsoft 365 work or school | `microsoft` | OAuth2 with the public Thunderbird client      |
| Personal Outlook.com         | `microsoft` | OAuth2 with the public Thunderbird client      |
| Gmail                        | `google`    | OAuth2 with the public Thunderbird client, or an app password |
| Other IMAP servers           | `imap`      | A password from a file                         |

The service does its own OAuth2 sign-in. It never reads the tokens of another
mail client. The Microsoft sign-in logs show the app as Mozilla Thunderbird.

## What the service downloads

The service never downloads a complete mailbox:

- It reads only the messages of the sync window (7 days by default), with
  `UID SEARCH SINCE`.
- For each message, it reads the flags, the size, the MIME structure, six
  header fields and a text preview of a few KiB.
- It downloads the full text or an attachment only when a tool asks for it,
  and an attachment only as its own MIME part.
- Every read uses `BODY.PEEK`, so reading never marks mail as read.
- It opens folders read-only, except for a read-state change (one `UID
  STORE` of `\Seen`) or an explicit `mail_archive`/`hermes-mail archive`
  call. Archiving moves a message with `UID MOVE`, or, on a server without
  it, `UID COPY` then `UID STORE +\Deleted` then `UID EXPUNGE` of exactly
  those UIDs. It is the only path that removes, moves or expunges mail, and
  only for the messages a caller names; nothing else does.

## Install

The step-by-step guide for Fedora Server is
[docs/INSTALL.md](docs/INSTALL.md). In short:

1. `pip install .` into a venv, for example `/opt/hermes-mail`.
2. Create the `hermes-mail` user with `deploy/sysusers.d/hermes-mail.conf`
   and the directories with `deploy/tmpfiles.d/hermes-mail.conf`.
3. Write `/etc/hermes-mail/config.json` from `deploy/config.example.json`.
4. Install `deploy/hermes-mail.service` and start it.
5. Build the plugin with `scripts/build-plugin.sh` and install it in Hermes.

The service runs under systemd with hardening: it can reach the network, its
socket and its state directory, and nothing more.

### Configuration

The service reads one JSON file. The example in `deploy/config.example.json`
shows every key:

- `state_dir`: the index, the cache and the sign-in tokens.
- `socket`: the service socket. The default is `/run/hermes-mail/mail.sock`.
- `export_dir`: where `export-attachment` saves files.
- `extract_root`: `extract-attachment` writes only below this directory.
  `null` turns the command off.
- `triage_retention_days`: how many days the triage log is kept. The default
  is 30, and `0` keeps it for ever.
- `web_settings`: let the Mail tab of the Hermes dashboard change the
  accounts. The default is `true`.
- `accounts.<name>`: `provider`, `address`, `auth`, `host`, `port`,
  `password_file`, `folders`, `archive_folder`, `sync_days`, `poll_seconds`,
  `cache_limit_mb`, `max_part_mb` and `notify` with `mode`, `target`,
  `policy_file`, `policy` and `mark_read_silent`.

`archive_folder` defaults to `Archive` for `microsoft` and
`[Gmail]/All Mail` for `google`. The `imap` provider has no default; set it
to use archiving.

## Sign-in

Sign in to each OAuth account one time:

```sh
hermes-mail auth login university
```

1. Open the printed URL in a browser and sign in, with MFA if the account
   asks for it.
2. The browser goes to a `localhost` URL that does not load. This is expected.
3. Copy the full URL from the address bar and paste it into the command.

The account starts to sync in a few seconds. If a provider later refuses the
refresh token, the account stops, and the notifier sends one `Mail problem:`
message that says to sign in again.

You can also sign in on the Mail tab of the Hermes dashboard. The tab shows
the same steps.

## Hermes dashboard

The plugin adds a Mail tab to the Hermes dashboard. On this tab you can:

- Add, change and remove accounts, and set the password of a password
  account. For a saved account that is signed in, the archive folder is a
  drop-down and the synced folders are a multi-select, both read from the
  mail server with a read-only `LIST`. The tab falls back to text fields when
  the server cannot be reached.
- Sign in to an OAuth account.
- Change the notifications of each account: the mode, the target, the triage
  policy and `markReadSilent`.
- Set the provider and the model of the triage call.

The tab keeps each setting with its owner:

- The accounts belong to `hermes-maild`. The service keeps tab changes in
  `settings.json` in `stateDir`, on top of the accounts of the config file.
  A change takes effect at once, without a restart.
- The notifications and the triage model belong to the plugin. They are
  Hermes plugin settings in `plugins.entries.hermes-mail.settings`:
  `notify`, `triage_provider` and `triage_model`, declared in the
  `config_schema` of `plugin.yaml`. The notifier reads them for each new
  event. The Hermes Desktop and TUI show the same settings.

The tab writes plugin settings through Hermes, so a Hermes install in
managed mode must opt out with `HERMES_MANAGED=false`.

The config file is the base. A change on the tab replaces the settings of
that account from the config file. Use "Reset to base config" or "Use base
settings" to go back. A removed base account stays on the tab, and "Restore"
brings it back.

The service applies these rules to account changes from the socket:

- A password goes into `stateDir/passwords/<account>` with mode 0600. The
  socket cannot set a password file path.
- A password file from the config stays in use only while the provider, the
  address, the host and the port stay the same.
- An OAuth account always uses the host of its provider.

Set `web_settings` to `false` to keep the accounts only in the config file.
The notifications stay editable, because the plugin owns them.

A triage provider or model other than `auxiliary.hermes_mail_triage` needs
`plugins.entries.hermes-mail.llm.allow_provider_override` or
`allow_model_override` in the Hermes configuration.

## Command line

Every command prints JSON:

```sh
hermes-mail status
hermes-mail folders university
hermes-mail list --since 2d --unread
hermes-mail search "exam" --account university
hermes-mail show <mail-id>
hermes-mail attachments <mail-id>
hermes-mail export-attachment <mail-id> <index>
hermes-mail mark-read <mail-id> [<mail-id> ...]
hermes-mail mark-unread <mail-id> [<mail-id> ...]
hermes-mail archive <mail-id> [<mail-id> ...]
hermes-mail triage-log [--account A] [--status S] [--since 2d] [--query text]
hermes-mail triage-show <entry-id>
hermes-mail triage-retry <entry-id>
hermes-mail events
```

## Notifications

The `hermes-mail-notify` unit runs `hermes mail notify` as the Hermes user.
Each account has a `notify.mode`:

- `none`: send nothing. The tools still read the account.
- `triage`: one LLM call classifies each new message. Code does the rest.
- `agent`: a Hermes agent run handles each new message with its own tools.

### Triage mode

For each new message of an account with `notify.mode = "triage"`:

1. One structured LLM call classifies the message as `notify` or `silent` and
   writes a short English summary. The call uses no tools.
2. Code does the actions: mark read (for `silent`, with `markReadSilent`) and
   export the useful attachments.
3. Code sends one message to `notify.target`: the subject, the sender, the
   summary and the attachments. The model's reasoning never reaches the chat.
4. Code reports each failed step with a `Mail problem:` line.

The triage uses the auxiliary task `hermes_mail_triage`, so it can use a
different model from the main agent:

```yaml
auxiliary:
  hermes_mail_triage:
    provider: opencode-go
    model: glm-5.3-flash
```

With no setting, it uses the main model.

To test the classifier on one message without any change:

```sh
hermes mail triage <mail-id>
```

### Agent mode

Use `notify.mode = "agent"` when the policy must do more than notify, for
example create a to-do task or a calendar entry for assigned work. The
notifier sends each new message to the Hermes gateway, and a normal agent run
follows the policy with the tools you give it.

1. The notifier writes a `dispatched` entry in the [activity log](#activity-log)
   and sends a signed event to the `hermes-mail` route of the Hermes webhook
   platform. The event has the account, the mail ID, a run ID and the policy
   text, but no mail text; the agent reads the mail with `mail_show`.
2. The agent follows the policy, uses its tools, and ends with one
   `mail_triage_report` call: the decision (`notify` or `silent`), a summary,
   the attachments to send and one entry for each action it took.
3. The report is the only way out. The route delivers to `log`, so the
   interim messages and the reasoning of the run never reach a chat. Code
   handles the report: it marks read, exports the attachments and sends one
   message to `notify.target`. The model never picks the target. A failed
   action of the agent goes into the message as a `Mail problem:` line.
4. A run that does not report within `agent_timeout_minutes` (15 by default)
   gets a `Mail problem:` message and the status `no_report`.

Set it up once:

1. Put a secret in the Hermes environment, for both the gateway and the
   notifier: `WEBHOOK_SECRET=<long random text>` in `~/.hermes/.env`. The
   notifier also reads `HERMES_MAIL_WEBHOOK_SECRET` or the plugin setting
   `agent_webhook_secret`.
2. Run `hermes mail agent-route`, and add its output to the `config.yaml` of
   Hermes under `platforms.webhook`. Then restart the gateway.
3. Set `notify.mode` to `agent` for the account, on the Mail tab or in the
   config. The tab checks if the gateway and the route are ready.

The route, and so its tools, belong to the Hermes config on purpose. Hermes
does not let a plugin or the dashboard grant tools to a webhook route. The
route from `hermes mail agent-route` has the toolsets `mail`, `mail_triage`
and `skills`. Add what your policy needs, for example `terminal` for a task
helper such as `hermes-todo`. Mind that the text of an email can steer the
tools of the run: give it only what it needs, and describe the allowed
actions in the policy.

The plugin settings `agent_webhook_url` (the default is
`http://127.0.0.1:8644/webhooks/hermes-mail`) and `agent_timeout_minutes`
change the address and the time limit.

### Activity log

The notifier writes one entry in the triage log for each new message it
handles in `triage` or `agent` mode: the account, the subject and the sender,
the decision (`notified`, `silent`, `error`, `dispatched` or `no_report`), the
reason, the summary, each action with its result (mark read, attachment
exports, and in agent mode the actions the agent reported) and any error. The
log is in the service, so you can read it without a chat:

- The Activity section of the Mail tab lists the entries with filters, shows
  the details of each, and has a "Process again" button that queues the mail
  for the notifier again.
- `hermes-mail triage-log`, `triage-show` and `triage-retry` do the same on
  the command line.
- The `mail_triage_log` tool lets the agent answer "what happened to that
  mail?" in any chat.

The log never holds the mail text. It keeps `triage_retention_days` days (30
by default), and an entry stays readable after its mail leaves the sync
window, but "Process again" needs the mail to be in the index.

### Upgrading from `all` mode and the task command

`notify.mode = "all"` and `notify.task_command` were removed. The service
refuses `all` with a message that says what to use. Use `triage` with a policy
that notifies for everything, or `agent`. A leftover `task_command` is ignored
with a warning; in `agent` mode, the agent creates tasks with its own tools.

## Development

```sh
make test          # the full suite on Python 3.14, the plugin test on Python 3.12
make lint          # ruff, also on the plugin code (target py312)
make build-plugin  # assemble dist/hermes-mail-plugin
```

The tests use an in-process fake IMAP server that records every command. They
fail when the service fetches a full message, uses `BODY` without `PEEK`, or
sends a bare `CLOSE` or `EXPUNGE`. A targeted `UID EXPUNGE` from
`mail_archive`'s fallback path is the only exception, and only for the UIDs
named in that call.

`scripts/probe.py` is the phase 0 probe. It checks if an account allows direct
IMAP access, and it changes nothing except one reversible read-state test:

```sh
python3 scripts/probe.py microsoft --user you@example.edu
```

`nix/tests/vm.nix` runs Dovecot and the service in a NixOS VM. It needs nix
and KVM:

```sh
nix build .#vm-test -L
```
