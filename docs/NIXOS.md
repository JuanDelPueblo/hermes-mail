# NixOS module

The flake has a NixOS module that runs the service and connects it to Hermes.
The standalone install in [INSTALL.md](INSTALL.md) needs no nix. Use only one
of the two paths on one host.

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
- `webSettings`: let the Mail tab of the Hermes dashboard change the
  accounts. The default is `true`.

After the first deployment, enable the plugin in Hermes and restart it:

```sh
hermes plugins enable hermes-mail
```

The plugin is not in `plugins.enabled` by default, because that list is
runtime Hermes config.

The module writes the service config as JSON, so the Mail tab of the Hermes
dashboard works the same way as with the standalone install: a change on the
tab replaces the settings of that account of the generated config, and "Reset
to base config" goes back.

The tab writes plugin settings through Hermes, so a Nix-managed Hermes
install must opt out of managed mode with `HERMES_MANAGED=false`.
