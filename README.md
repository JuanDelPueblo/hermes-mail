# hermes-mail

IMAP mail access for the [Hermes agent](https://github.com/NousResearch/hermes-agent),
with no desktop mail client. The repository has three parts:

- `hermes-maild`: a small service that keeps an index of recent mail and
  talks IMAP. The `hermes-mail` command is its client.
- A Hermes plugin with `mail_*` tools, a mail skill and a notifier that
  triages new mail into chat messages.
- A NixOS module that runs the service and connects it to Hermes.

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

## NixOS

Add the flake input and import the module:

```nix
{
  inputs.hermes-mail.url = "github:JuanDelPueblo/hermes-mail";

  # In a NixOS module:
  imports = [ inputs.hermes-mail.nixosModules.default ];
}
```

Example configuration:

```nix
services.hermes-mail = {
  enable = true;
  accounts = {
    university = {
      provider = "microsoft";
      address = "student@example.edu";
      notify = {
        mode = "triage";
        target = "discord:1234567890";
        policyFile = ./university-mail-policy.md;
        markReadSilent = true;
      };
    };
    outlook = { provider = "microsoft"; address = "someone@outlook.com"; };
    gmail = { provider = "google"; address = "someone@gmail.com"; };
  };
  # Adds the plugin and the CLI to services.hermes-agent and runs the notifier.
  hermes.enable = true;
};
```

Important options:

- `user`, `group`: the service identity. The default is a `hermes-mail`
  system user. The Hermes user needs the group to use the socket at
  `/run/hermes-mail/mail.sock`. With `hermes.enable`, the module adds the
  Hermes user to the group.
- `stateDir`: the index, the cache and the sign-in tokens (mode 0600).
- `exportDir`: where `export-attachment` saves files for chat uploads.
- `extractRoot`: `extract-attachment` writes only below this directory.
- `accounts.<name>`: `provider`, `address`, `auth`, `host`, `port`,
  `passwordFile`, `folders`, `archiveFolder`, `syncDays`, `pollSeconds`,
  `cacheLimitMB`, `maxPartMB` and `notify`. `archiveFolder` defaults to
  `Archive` for `microsoft` and `[Gmail]/All Mail` for `google`; the `imap`
  provider has no default.

After the first deployment, enable the plugin in Hermes and restart it:

```sh
hermes plugins enable hermes-mail
```

The plugin is not in `plugins.enabled` by default, because that list is
runtime Hermes config.

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

## Command line

Every command prints JSON:

```sh
hermes-mail status
hermes-mail list --since 2d --unread
hermes-mail search "exam" --account university
hermes-mail show <mail-id>
hermes-mail attachments <mail-id>
hermes-mail export-attachment <mail-id> <index>
hermes-mail mark-read <mail-id> [<mail-id> ...]
hermes-mail mark-unread <mail-id> [<mail-id> ...]
hermes-mail archive <mail-id> [<mail-id> ...]
hermes-mail events
```

## Notifications

The `hermes-mail-notify` unit runs `hermes mail notify` as the Hermes user.
For each new message of an account with `notify.mode = "triage"`:

1. One structured LLM call classifies the message as `notify` or `silent` and
   writes a short English summary. The call uses no tools.
2. Code does the actions: mark read (for `silent`, with `markReadSilent`),
   export the useful attachments and run the task command.
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

`notify.mode = "all"` sends every message with a short preview, with no LLM.
`notify.mode = "none"` sends nothing.

To test the triage of one message without any change:

```sh
hermes mail triage <mail-id>
```

### Task command

`notify.taskCommand` is an optional command that creates a task when the
triage finds assigned work. The notifier runs it with a JSON object on stdin:

```json
{"title": "...", "due": "YYYY-MM-DD or null", "list": null, "notes": null,
 "mail_id": "...", "message_id": "...", "subject": "...", "sender": "...", "account": "..."}
```

A zero exit status means success. The first line of the output goes into the
notification. With a non-zero exit status, the last line of stderr goes into a
`Mail problem:` line.

## Development

```sh
nix flake check                 # unit tests, lint, module evaluation, formatting
nix build .#vm-test -L          # Dovecot and the service in a NixOS VM (needs KVM)
nix fmt
```

The tests use an in-process fake IMAP server that records every command. They
fail when the service fetches a full message, uses `BODY` without `PEEK`, or
sends a bare `CLOSE` or `EXPUNGE`. A targeted `UID EXPUNGE` from
`mail_archive`'s fallback path is the only exception, and only for the UIDs
named in that call.

`scripts/probe.py` is the phase 0 probe. It checks if an account allows direct
IMAP access, and it changes nothing except one reversible read-state test:

```sh
nix shell nixpkgs#python314 --command python3 scripts/probe.py microsoft --user you@example.edu
```
