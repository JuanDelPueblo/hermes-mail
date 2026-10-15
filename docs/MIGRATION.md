# Migrating from hermes-mail to uni-mail

uni-mail is hermes-mail with the Hermes parts removed and a generic event
dispatcher and MCP server added. The mail daemon, the OAuth sign-in, the
index format and the mail IDs are the same, so an installation moves over
without a new sign-in.

## What changed

| hermes-mail | uni-mail |
| --- | --- |
| Python package `hermes_mail`, distribution `hermes-mail` | `uni_mail`, `uni-mail` |
| `hermes-maild`, `hermes-mail` | `uni-maild`, `uni-mail`, plus `uni-mail-mcp` (extra `mcp`) |
| `hermes-mail.service` | `uni-mail.service` (has `Conflicts=hermes-mail.service hermes-mail-notify.service`) |
| `hermes-mail-notify.service` (`hermes mail notify`) | `uni-mail-dispatch.service` (`uni-mail dispatch run`) |
| `/etc/hermes-mail/config.json` | `/etc/uni-mail/config.json` |
| `/var/lib/hermes-mail` | `/var/lib/uni-mail`, and `/var/lib/uni-mail-dispatch` for the dispatcher |
| `/run/hermes-mail/mail.sock` | `/run/uni-mail/mail.sock` |
| user and group `hermes-mail` | `uni-mail`, and `uni-mail-dispatch` |
| `HERMES_MAIL_SOCKET`, `HERMES_MAIL_LOG_LEVEL` | `UNI_MAIL_SOCKET`, `UNI_MAIL_LOG_LEVEL`; also `UNI_MAIL_CONFIG` |
| Socket ops `events` (pending only) and `ack` | `events` with a cursor (`after`), `event_head`, `events_consumed` |
| `hermes-mail triage-log`, `triage-show`, `triage-retry` | `uni-mail dispatch list`, `show`, `retry` |
| Hermes dashboard Mail tab | `uni-mail account list/set/remove/reset` and `uni-mail auth login` |
| NixOS module and flake | removed |

Unchanged: the token files (`tokens/<account>.json`, mode 0600), the account
changes (`settings.json`), the stored passwords (`passwords/`), the index
(`index.sqlite3`), the mail IDs and the folder baselines.

## What happened to the Hermes plugin, the notifier and triage

They are gone. uni-mail makes no decision about mail; the program that
receives its events does.

| Removed | Replacement |
| --- | --- |
| The Hermes plugin (`mail_*` tools, `mail` skill, dashboard tab) | `uni-mail-mcp`: the same mail tools for any MCP client |
| `notify.mode = "triage"` (LLM classifier) and `"agent"` (Hermes webhook run) | A dispatch destination that hands the event to your agent, for example `integrations/nanobot` |
| `notify.policy` / `policy_file` | Your agent's own instructions |
| `notify.target` (chat delivery, attachment forwarding) | Your agent decides where to write; a Nanobot trigger is bound to a chat |
| `notify.mark_read_silent` | Your agent calls `mail_mark_read` explicitly. uni-mail never changes the read state on its own |
| `mail_triage_report`, run IDs, the agent timeout sweeper | Nothing: the dispatcher records each delivery attempt instead |
| `triage_retention_days` | `event_retention_days` (daemon) and `dispatch.retention_days` |
| `auxiliary.hermes_mail_triage`, `triage_provider`, `triage_model`, `agent_webhook_url`, `WEBHOOK_SECRET` | Nothing |

`uni-maild` still starts with a config that has `notify` or
`triage_retention_days`: it logs a warning and ignores them. `uni-mail
migrate` removes them and lists them in its report.

The `triage` table of the index is not touched. Its rows stay in the copied
index as inert data that no code reads; query it with `sqlite3` if you need
the history, or drop it.

## Before you start

- Note the pending events: `hermes-mail events`. They are delivered by the
  uni-mail dispatcher after the move.
- Back up the old state and config. The migration copies and never changes
  them, but a backup costs nothing:

  ```sh
  sudo tar -C / -czf /root/hermes-mail-backup.tgz etc/hermes-mail var/lib/hermes-mail
  ```

## Steps (Fedora with systemd)

1. Install uni-mail next to hermes-mail:

   ```sh
   sudo python3 -m venv /opt/uni-mail
   sudo /opt/uni-mail/bin/pip install '/path/to/uni-mail[mcp]'
   sudo cp deploy/sysusers.d/uni-mail.conf /etc/sysusers.d/
   sudo systemd-sysusers /etc/sysusers.d/uni-mail.conf
   sudo cp deploy/tmpfiles.d/uni-mail.conf /etc/tmpfiles.d/
   sudo systemd-tmpfiles --create /etc/tmpfiles.d/uni-mail.conf
   sudo cp deploy/uni-mail.service deploy/uni-mail-dispatch.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

2. Stop hermes-mail. Both units must stay off: two daemons on copies of the
   same state would both sync and both create events.

   ```sh
   sudo systemctl disable --now hermes-mail-notify.service hermes-mail.service
   ```

3. Check the plan, then copy:

   ```sh
   sudo /opt/uni-mail/bin/uni-mail migrate            # report only
   sudo /opt/uni-mail/bin/uni-mail migrate --apply
   ```

   The command refuses to run while a hermes-mail daemon answers or a
   hermes-mail unit is active, when `/etc/uni-mail/config.json` exists, and
   when `/var/lib/uni-mail` already holds an index, tokens or settings. It
   copies the index with the SQLite backup API and checks it, copies the
   tokens, passwords and `settings.json` with mode 0600, copies the password
   files that the config names below `/etc/hermes-mail`, gives the state to
   `uni-mail` and the config to `root:uni-mail` with mode 0640, and writes
   `MIGRATED-FROM-HERMES-MAIL.json` in the new state. Add `--with-cache` to
   also copy the part cache and the exports (they are fetched again when
   needed). Use `--from-config`, `--to-config`, `--state-dir` and `--owner`
   for other paths.

4. Start the daemon and check that every account reaches `idle` without a
   new sign-in:

   ```sh
   sudo systemctl enable --now uni-mail.service
   uni-mail status
   uni-mail list --since 1d        # the same mail IDs as before
   ```

   On its first start, the daemon upgrades the index in place: it adds an
   `event_id` and a `data` column to the event log and gives every old event
   an ID (`PRAGMA user_version` 2). The old code can still read the upgraded
   file.

5. Give the users of the old socket access to the new one, for example the
   user that runs Nanobot: `sudo usermod -aG uni-mail <user>` (log in again).

6. Add destinations to the `dispatch` section (docs/EVENTS.md,
   integrations/nanobot/README.md), then:

   ```sh
   sudo -u uni-mail-dispatch /opt/uni-mail/bin/uni-mail dispatch check
   sudo systemctl enable --now uni-mail-dispatch.service
   uni-mail dispatch status
   ```

   The first dispatcher run starts at the first event that hermes-mail had
   not acknowledged, so those events are delivered once; the history is not.

7. Remove the Hermes side:
   - `hermes plugins disable hermes-mail` and delete the plugin directory
     (for example `~/.hermes/plugins/hermes-mail`);
   - delete the `hermes-mail` route under `platforms.webhook` in Hermes'
     `config.yaml`, `auxiliary.hermes_mail_triage`, and
     `plugins.entries.hermes-mail`; restart the gateway;
   - delete `/etc/systemd/system/hermes-mail*.service` and its drop-ins, run
     `systemctl daemon-reload`.

8. After some days without problems: delete `/opt/hermes-mail`,
   `/etc/hermes-mail`, `/var/lib/hermes-mail`, and the `hermes-mail` user and
   group.

## No duplicate or lost events

- Mail already in the index creates no event: the folder baselines are
  copied, so the first sync of uni-maild only finds mail it does not know.
- Mail that arrives while both daemons are stopped is newer than the copied
  index, so uni-maild finds it on its first sync and creates `mail.new` for it.
- Unacknowledged hermes-mail events are copied with the index and delivered
  once by a fresh dispatcher (it starts at the first event with `done = 0`).
  hermes-mail acknowledged events one by one, so an event that it had already
  acknowledged after the first unacknowledged one is delivered again (with
  its new event ID). Check `uni-mail events` before you start the dispatcher
  if that matters.
- uni-maild refuses to start while a hermes-mail daemon answers on
  `/run/hermes-mail/mail.sock` (override: `UNI_MAIL_ALLOW_LEGACY=1`), the unit
  conflicts with the hermes-mail units, and a lock file stops a second
  uni-maild on the same state directory.

## Rollback

The hermes-mail state is unchanged, so going back is a restart, with one
catch: the providers can rotate the refresh token. If uni-mail received a new
refresh token, the old copy can stop working. Copy the current tokens back
first.

```sh
sudo systemctl disable --now uni-mail-dispatch.service uni-mail.service
sudo cp -p /var/lib/uni-mail/tokens/*.json /var/lib/hermes-mail/tokens/
sudo chown hermes-mail:hermes-mail /var/lib/hermes-mail/tokens/*.json
sudo chmod 0600 /var/lib/hermes-mail/tokens/*.json
sudo systemctl enable --now hermes-mail.service hermes-mail-notify.service
```

Mail that arrived while uni-mail ran is new to the old index, so hermes-mail
creates events for it again. Keep or delete the uni-mail state; a later
migration needs `/etc/uni-mail/config.json` and `/var/lib/uni-mail` moved
away first.

## Manual recovery

- **The migration stopped halfway.** It writes the config last. Delete the
  files it created (`/etc/uni-mail/config.json` if present, and the contents
  of `/var/lib/uni-mail` and of the new password files), then run it again.
  The source was never changed.
- **The index is damaged.** Stop uni-mail and move `index.sqlite3` (with
  `-wal` and `-shm`) away. The daemon builds a new index on start, as a
  first sync: no events for the existing mail, and the same mail IDs (they
  are a hash of the account, folder, `UIDVALIDITY` and UID).
- **A token is missing or refused.** `uni-mail auth login <account>`.
- **The dispatch database is damaged.** Stop the dispatcher and move
  `/var/lib/uni-mail-dispatch/dispatch.sqlite3` away. A new database starts
  at the first event that no dispatcher stored; check
  `uni-mail dispatch status` and use `uni-mail dispatch replay` for anything
  you want delivered again.

## The GitHub repository rename

Rename `JuanDelPueblo/hermes-mail` to `JuanDelPueblo/uni-mail` last, after
the code is merged:

1. Resolve the open stack in order: PR #7 (`agent-triage` into `main`), then
   this PR (`refactor/uni-mail-provider` into `agent-triage`). When the base
   of a PR is merged and its branch deleted, GitHub moves the next PR to
   `main` by itself. Alternatively merge this PR into `agent-triage` first and
   then PR #7 into `main` as one change.
2. Rename the repository in Settings → General. GitHub keeps the PRs, issues,
   branches and stars, and redirects the old URL for web pages and for
   `git fetch`/`git push`.
3. Update every clone: `git remote set-url origin git@github.com:JuanDelPueblo/uni-mail.git`.
4. Do not create a new repository named `hermes-mail` afterwards: it would
   break the redirect.
