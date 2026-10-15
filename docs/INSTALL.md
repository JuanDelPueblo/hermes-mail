# Install on Fedora Server

This guide installs `uni-maild`, the `uni-mail` command, the Hermes
plugin and the notifier on Fedora Server 44. The service needs Python 3.14 or
later. Fedora Server 44 ships it as `python3`. Check it first:

```sh
python3 --version
```

## 1. Install the Python package

Run the commands from a checkout of this repository:

```sh
sudo dnf install python3
sudo python3 -m venv /opt/uni-mail
sudo /opt/uni-mail/bin/pip install .
```

With no network access, install the build backend from Fedora and add
`--no-build-isolation`:

```sh
sudo dnf install python3-setuptools
sudo /opt/uni-mail/bin/pip install --no-build-isolation .
```

## 2. Create the service user and the directories

```sh
sudo cp deploy/sysusers.d/uni-mail.conf /etc/sysusers.d/
sudo systemd-sysusers /etc/sysusers.d/uni-mail.conf
sudo cp deploy/tmpfiles.d/uni-mail.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/uni-mail.conf
```

This creates the `uni-mail` user and group, `/var/lib/uni-mail` and
`/var/lib/uni-mail/exports`, both with mode 0750.

## 3. Write the config

```sh
sudo mkdir -p /etc/uni-mail
sudo cp deploy/config.example.json /etc/uni-mail/config.json
```

Then edit `/etc/uni-mail/config.json`:

- Set the accounts. One account per entry of `accounts`.
- For a `triage` or `agent` notify mode, set `notify.target`.
- Put the triage policy of each account in its own file, for example
  `/etc/uni-mail/university-policy.md`, and set `notify.policy_file`.
- Put the password of a password account in its own file, for example
  `/etc/uni-mail/passwords/selfhosted`. Give it mode 0600:

```sh
sudo chown uni-mail:uni-mail /etc/uni-mail/passwords/selfhosted
sudo chmod 0600 /etc/uni-mail/passwords/selfhosted
```

The service validates the file at start and prints a clear error for each
problem.

## 4. Run the service

```sh
sudo cp deploy/uni-mail.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now uni-mail.service
/opt/uni-mail/bin/uni-mail status
```

The unit runs `/opt/uni-mail/bin/uni-maild` with
`/etc/uni-mail/config.json`. To change a path or the log level, write a
drop-in in `/etc/systemd/system/uni-mail.service.d/` and run
`systemctl daemon-reload`.

The unit is hardened: the service can reach the network, its socket and its
state directory, and nothing more. When you set `extract_root` in the config,
also add that path to `ReadWritePaths` in a drop-in, and change
`ProtectHome=true` to `ProtectHome=read-only` when the path is below `/home`.

## 5. Give Hermes access to the socket

```sh
sudo ln -s /opt/uni-mail/bin/uni-mail /usr/local/bin/uni-mail
sudo usermod -aG uni-mail <hermes-user>
```

Log in again, so the group membership applies. Members of the group can use
the socket at `/run/uni-mail/mail.sock` and read the exported attachments.

## 6. Install the plugin

```sh
scripts/build-plugin.sh
hermes plugins install "file://$PWD/dist/uni-mail-plugin"
hermes plugins enable uni-mail
```

Hermes installs plugins from git sources only. The build script writes a git
repository into `dist/uni-mail-plugin`, so the `file://` path works.

Then restart Hermes. Do not install the whole repository as a plugin: its
security scan finds the public Thunderbird client secret in the service code.

## 7. Run the notifier

First set the values of your Hermes install in
`deploy/uni-mail-notify.service`: the path of the `hermes` command, the
user, the group, the working directory and `HERMES_HOME`. The notifier needs
the same identity and environment as the Hermes gateway, so it reads the same
config, credentials and plugins.

```sh
sudo cp deploy/uni-mail-notify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now uni-mail-notify.service
```

## 8. Sign in and verify

```sh
uni-mail auth login university
uni-mail status
hermes mail triage <mail-id>
```

The last command tests the triage of one message and changes nothing.

## SELinux

Fedora runs SELinux. When the service does not start, read its log:

```sh
journalctl -u uni-mail.service
sudo ausearch -m avc -ts recent
```

When the log shows a denial for `/opt/uni-mail`, fix the file labels and
start again:

```sh
sudo restorecon -RFv /opt/uni-mail
```

## Move the state from NixOS

The state directory holds the index, the cache and the sign-in tokens. Copy it
with its modes, then the accounts need no new sign-in:

```sh
sudo rsync -a --info=progress2 old-host:/var/lib/uni-mail/ /var/lib/uni-mail/
```
