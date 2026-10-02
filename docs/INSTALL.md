# Install on Fedora Server

This guide installs `hermes-maild`, the `hermes-mail` command, the Hermes
plugin and the notifier on Fedora Server 44. The service needs Python 3.14 or
later. Fedora Server 44 ships it as `python3`. Check it first:

```sh
python3 --version
```

## 1. Install the Python package

Run the commands from a checkout of this repository:

```sh
sudo dnf install python3
sudo python3 -m venv /opt/hermes-mail
sudo /opt/hermes-mail/bin/pip install .
```

With no network access, install the build backend from Fedora and add
`--no-build-isolation`:

```sh
sudo dnf install python3-setuptools
sudo /opt/hermes-mail/bin/pip install --no-build-isolation .
```

## 2. Create the service user and the directories

```sh
sudo cp deploy/sysusers.d/hermes-mail.conf /etc/sysusers.d/
sudo systemd-sysusers /etc/sysusers.d/hermes-mail.conf
sudo cp deploy/tmpfiles.d/hermes-mail.conf /etc/tmpfiles.d/
sudo systemd-tmpfiles --create /etc/tmpfiles.d/hermes-mail.conf
```

This creates the `hermes-mail` user and group, `/var/lib/hermes-mail` and
`/var/lib/hermes-mail/exports`, both with mode 0750.

## 3. Write the config

```sh
sudo mkdir -p /etc/hermes-mail
sudo cp deploy/config.example.json /etc/hermes-mail/config.json
```

Then edit `/etc/hermes-mail/config.json`:

- Set the accounts. One account per entry of `accounts`.
- For a `triage` or `all` notify mode, set `notify.target`.
- Put the triage policy of each account in its own file, for example
  `/etc/hermes-mail/university-policy.md`, and set `notify.policy_file`.
- Put the password of a password account in its own file, for example
  `/etc/hermes-mail/passwords/selfhosted`. Give it mode 0600:

```sh
sudo chown hermes-mail:hermes-mail /etc/hermes-mail/passwords/selfhosted
sudo chmod 0600 /etc/hermes-mail/passwords/selfhosted
```

The service validates the file at start and prints a clear error for each
problem.

## 4. Run the service

```sh
sudo cp deploy/hermes-mail.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-mail.service
/opt/hermes-mail/bin/hermes-mail status
```

The unit runs `/opt/hermes-mail/bin/hermes-maild` with
`/etc/hermes-mail/config.json`. To change a path or the log level, write a
drop-in in `/etc/systemd/system/hermes-mail.service.d/` and run
`systemctl daemon-reload`.

The unit is hardened: the service can reach the network, its socket and its
state directory, and nothing more. When you set `extract_root` in the config,
also add that path to `ReadWritePaths` in a drop-in, and change
`ProtectHome=true` to `ProtectHome=read-only` when the path is below `/home`.

## 5. Give Hermes access to the socket

```sh
sudo ln -s /opt/hermes-mail/bin/hermes-mail /usr/local/bin/hermes-mail
sudo usermod -aG hermes-mail <hermes-user>
```

Log in again, so the group membership applies. Members of the group can use
the socket at `/run/hermes-mail/mail.sock` and read the exported attachments.

## 6. Install the plugin

```sh
scripts/build-plugin.sh
hermes plugins install "file://$PWD/dist/hermes-mail-plugin"
hermes plugins enable hermes-mail
```

Hermes installs plugins from git sources only. The build script writes a git
repository into `dist/hermes-mail-plugin`, so the `file://` path works.

Then restart Hermes. Do not install the whole repository as a plugin: its
security scan finds the public Thunderbird client secret in the service code.

## 7. Run the notifier

First set the values of your Hermes install in
`deploy/hermes-mail-notify.service`: the path of the `hermes` command, the
user, the group, the working directory and `HERMES_HOME`. The notifier needs
the same identity and environment as the Hermes gateway, so it reads the same
config, credentials and plugins.

```sh
sudo cp deploy/hermes-mail-notify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-mail-notify.service
```

## 8. Sign in and verify

```sh
hermes-mail auth login university
hermes-mail status
hermes mail triage <mail-id>
```

The last command tests the triage of one message and changes nothing.

## SELinux

Fedora runs SELinux. When the service does not start, read its log:

```sh
journalctl -u hermes-mail.service
sudo ausearch -m avc -ts recent
```

When the log shows a denial for `/opt/hermes-mail`, fix the file labels and
start again:

```sh
sudo restorecon -RFv /opt/hermes-mail
```

## Move the state from NixOS

The state directory holds the index, the cache and the sign-in tokens. Copy it
with its modes, then the accounts need no new sign-in:

```sh
sudo rsync -a --info=progress2 old-host:/var/lib/hermes-mail/ /var/lib/hermes-mail/
```
