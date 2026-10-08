# hermes-mail plan

hermes-mail gives a Hermes agent access to mail over IMAP, with no desktop
mail client. It has these parts:

- `hermes-maild`, a small service that keeps a local index of recent mail.
- A Hermes plugin with typed mail tools and a generic mail skill.
- Deployment files for systemd: units, sysusers, tmpfiles and a config
  example. See docs/INSTALL.md.
- A NixOS module that runs the service and connects it to Hermes. See
  docs/NIXOS.md.

## Accounts

The first version supports these account types:

| Account                          | Provider preset | Sign-in                                   |
| -------------------------------- | --------------- | ----------------------------------------- |
| Work or school Microsoft 365     | `microsoft`     | OAuth2 with the Thunderbird public client |
| Personal Outlook.com             | `microsoft`     | OAuth2 with the Thunderbird public client |
| Gmail                            | `google`        | OAuth2 with the Thunderbird public client, or an app password |
| Other IMAP servers               | `imap`          | Password from a file                      |

The service signs in with its own OAuth2 authorization. It never reads the
tokens of another mail client. The operator signs in one time in a browser,
with MFA when the account asks for it, and pastes the redirect URL into
`hermes-mail auth login <account>`. Device-code sign-in is not used, because
restrictive tenants often block it.

The refresh token is mutable runtime state. The service keeps it in the state
directory with mode 0600. It is never in a config file, in the logs or in a
socket response, so the plugin and the agent never see a token.

When a refresh fails, the service stops that account and sends one
`mail.auth` event. It does not retry in a loop, because each failure can count
against the account.

## Download limits

The service must never download a complete mailbox. These rules apply to
every account:

1. Each account has a sync window, `syncDays`, with a default of 7 days. The
   service finds messages with `UID SEARCH SINCE`. It never reads older mail.
2. The index stores only `UID`, `FLAGS`, `INTERNALDATE`, `RFC822.SIZE`,
   `BODYSTRUCTURE`, six header fields (Date, From, To, Cc, Subject and
   Message-ID) and a text preview of a maximum of 4000 characters. The preview
   is a partial fetch (`BODY.PEEK[<part>]<0.n>`) of the text part.
3. The service downloads a full text part only when a tool asks for the
   message. It downloads an attachment by its MIME part, never as the full
   message.
4. Every read uses `BODY.PEEK`, so a read never marks mail as read.
5. The message cache has a size limit for each account, and the service
   removes cached data when a message leaves the sync window.
6. A folder is opened read-only (`EXAMINE`) except for a read-state change or
   an explicit archive action. Only `mail_archive`/`hermes-mail archive`
   moves, deletes or expunges mail, and only the UIDs it names: `UID MOVE`
   when the server has it, otherwise `UID COPY` + `UID STORE +\Deleted` +
   `UID EXPUNGE` of exactly those UIDs. Nothing else does. The dashboard and
   `hermes-mail folders` also run a read-only `LIST` to show the folder names.
7. The tests record the IMAP commands of each operation. They fail on any
   `BODY[]`, `RFC822` or full-folder fetch, and on a bare `CLOSE` or
   `EXPUNGE` outside the archive fallback path.

## Service

- Python 3.14 standard library only. Python 3.14 includes IMAP IDLE.
- One IMAP connection per account in IDLE on `INBOX`. The service restarts
  IDLE before the server timeout, and does a full window check every few
  minutes, because a server can drop IDLE without notice.
- One SQLite index for all accounts. A mail ID has the form
  `<account>.<16 hex digits>`. The digits are a hash of the account, the
  folder, `UIDVALIDITY` and the UID, so the ID is stable. The index also keeps
  the `Message-ID` header for idempotent follow-up actions.
- On the first sync, the service records existing mail and sends no event.
- A Unix socket with a JSON line protocol. The `hermes-mail` CLI and the
  plugin use this socket.
- An event queue on the socket: `mail.new` for new mail, `mail.auth` when an
  account needs a new sign-in, and `mail.error` when an account cannot sync
  for 30 minutes. The notifier finishes each event with an acknowledgement.
- A read-state change is one `UID STORE` of `\Seen` on the server. An archive
  action is one `UID MOVE`, or, without it, `UID COPY` + `UID STORE
  +\Deleted` + `UID EXPUNGE` of exactly the named UIDs.

## Notifications

A Hermes agent run delivers all the text that the model makes. This text can
include reasoning, interim messages and partial answers. The model of the run
is the main model of the agent, and the display settings of the chat channel
apply to it. When the main model does not call tools, the agent sends its
plan and does no work.

For this reason, the plugin owns the triage and the delivery of new mail. An
agent run does not do these steps:

1. The plugin adds the `hermes mail notify` command. The
   `hermes-mail-notify` systemd unit runs it with the user and the
   environment of the Hermes gateway. It waits for events on the service
   socket. The service sends no webhook for new mail.
2. The plugin classifies each message with one structured LLM call
   (`ctx.llm.complete_structured`). The call uses the plugin's own auxiliary
   task, `hermes_mail_triage`, so `auxiliary.hermes_mail_triage` sets its
   model. The main model and the channel display settings do not apply. The
   call uses no tools, so any model with structured output can do it.
3. The result has a fixed schema: `notify` or `silent`, a reason, a summary,
   the attachment indexes to send, and an optional task.
4. The plugin code does the actions: mark read, export attachments, run the
   task command, and send one message for each notified mail. The message
   contains only the summary and the attachments.
5. The plugin code reports every failure with a `Mail problem:` message. If
   the classification fails, the plugin notifies with the sender and the
   subject, so no mail is lost without a message.
6. The plugin sends with the `send_message` tool of Hermes, the same path as
   `hermes send`. When a send fails, the notifier sends the same message
   again up to 8 times, with a delay from 5 seconds to 10 minutes. A retry
   does not run the triage, the export or the task command again.

Each account has a notification policy:

- `mode`: `triage`, `all` or `none`.
- `target`: the platform and chat that get the messages.
- `policyFile`: the triage rules, in the config file of the deployment.
- `markReadSilent`: mark mail as read when the triage says `silent`.
- `taskCommand`: an optional command for a task, for example a To Do helper.

Every handled event also writes an entry in the triage log of the service
(the `triage` table of the index, 30 days by default): the decision, the
reason, the summary, the actions and the errors, but never the mail text. The
Mail tab, `hermes-mail triage-log` and the `mail_triage_log` tool read it, so
the owner can see what the plugin did with a message without any chat
gateway. If the log cannot be written, the notification still goes out.

A later version can add a daily digest of silent mail. Questions about a mail
in the chat use the normal agent with the plugin tools. `hermes mail triage
<mail-id>` prints the triage result of one message and changes nothing.

## Hermes plugin

The plugin files are at the repository root. `scripts/build-plugin.sh`
assembles `dist/hermes-mail-plugin`, which holds only the files that Hermes
loads: the plugin, the skill and the socket client. The script writes a git
repository into the directory, because Hermes installs plugins from git
sources only. Hermes installs this directory as an extra plugin, with a
`file://` path. `hermes plugins install` of the whole repository is not
supported: its security scan finds the public Thunderbird client secret in
the service code and the probe.

Tools:

- `mail_status`: accounts, sign-in state, last sync, errors.
- `mail_list` and `mail_search`: filter by account, age, read state and text.
- `mail_show` and `mail_attachments`.
- `mail_export_attachment`: cache one attachment and return its path for a
  `MEDIA:` upload.
- `mail_mark_read` and `mail_mark_unread`: accept many mail IDs.
- `mail_archive`: moves messages to the account's `archiveFolder` and drops
  them from the local index. Not reversible from the tool.

Every tool returns `ok: false` and the error text on a failure. The skill tells
the agent to report every mail error. The CLI keeps the command names of the
current Thunderbird bridge during the change.

Personal triage rules, prompts and chat destinations are not part of this
repository. They stay in the config file of the deployment.

Dashboard: the plugin adds a Mail tab to the Hermes dashboard
(`dashboard/`), with its own API under `/api/plugins/hermes-mail/`. Each
setting stays with its owner:

- Account settings belong to the service. The tab changes them over the
  socket (`settings`, `settings_save`, `settings_delete`, `settings_reset`).
  The service keeps them in `settings.json` in the state directory, on top of
  the base accounts, and starts, restarts or stops only the changed workers.
  The socket cannot set a password file path or move an OAuth account to
  another host, so a socket user cannot send a stored secret to another
  server.
- Notification and triage settings belong to the plugin. They are Hermes
  plugin settings (`notify`, `triage_provider`, `triage_model` in the
  `config_schema`). The tab writes them with the Hermes plugin settings
  writer, and the notifier reads them with `ctx.get_config`. They stay out of
  the service, so a socket user cannot set the task command that the
  notifier runs as the Hermes user.

## NixOS module

```nix
services.hermes-mail = {
  enable = true;
  accounts = {
    university = {
      provider = "microsoft";
      address = "...";
      syncDays = 7;
      notify = {
        mode = "triage";
        target = "discord:<channel id>";
        policyFile = ./university-mail-policy.md;
        markReadSilent = true;
      };
    };
    outlook = { provider = "microsoft"; address = "..."; notify.mode = "none"; };
    gmail = { provider = "google"; address = "..."; notify.mode = "none"; };
  };
  hermes.enable = true;
};
```

- Each account can set `folders`, `archiveFolder`, `syncDays`, `pollSeconds`,
  `cacheLimitMB`, `maxPartMB` and `notify`. An account with `notify.mode =
  "none"` is indexed and the tools can use it, but it sends no messages.
  `archiveFolder` defaults to `Archive` for `microsoft` and `[Gmail]/All
  Mail` for `google`; `imap` has no default and needs one to use
  `mail_archive`.
- `hermes.enable` adds the plugin to `services.hermes-agent.extraPlugins` and
  the CLI to `services.hermes-agent.extraPackages`. With `hermes.notifier`
  (the default), it also runs the `hermes-mail-notify` unit.
- The systemd service is hardened. It can reach the network, its socket and
  its state directory, and nothing more.
- The flake has unit tests, a NixOS VM test against Dovecot and CI.

## Phases

0. Done. Probe (`scripts/probe.py`). Check the OAuth2 sign-in, the refresh
   grant, XOAUTH2, the sync window, `BODY.PEEK`, IDLE and the `\Seen` change
   on each account. Result: GO for the university account and for Gmail.
1. Done. Service and CLI, with tests against the fake IMAP server.
2. Done. Hermes plugin, skill, triage and notifications.
3. Done. NixOS module, VM test and CI.
4. Parallel run on the production host with `notify.mode = "none"`. Compare
   the index with the Thunderbird bridge for some days.
5. Turn on triage for the university account and remove the Hermes webhook
   route. Then remove the Thunderbird container, extension, native host and
   bridge from the host configuration.

## Risks

- The Microsoft sign-in logs show the app as Mozilla Thunderbird. A tenant can
  see this as the use of another app's identity, and it can block the app at
  any time. The probe shows the current state only.
- A tenant policy can require a new sign-in after some days. The `mail.auth`
  event tells the operator when this occurs.
- Google can limit the Thunderbird client for the `https://mail.google.com/`
  scope. An app password is the fallback for Gmail.
- Delivery is at least once. If the notifier stops after a send and before
  the acknowledgement, it sends that message again after the restart.
