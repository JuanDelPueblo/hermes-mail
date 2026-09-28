{
  pkgs,
  self,
  nixpkgs,
}:
let
  inherit (pkgs) lib;
  src = lib.cleanSource ../.;
  system = pkgs.stdenv.hostPlatform.system;
  plugin = self.packages.${system}.hermes-mail-plugin;

  # Stands in for the services.hermes-agent module, which is not an input of
  # this flake. It declares only the options that the integration uses.
  hermesStub =
    { lib, ... }:
    {
      options.services.hermes-agent = {
        user = lib.mkOption { default = "hermes"; };
        group = lib.mkOption { default = "hermes"; };
        stateDir = lib.mkOption { default = "/var/lib/hermes"; };
        package = lib.mkOption { default = pkgs.hello; };
        extraPlugins = lib.mkOption { default = [ ]; };
        extraPackages = lib.mkOption { default = [ ]; };
      };
      config.systemd.services.hermes-agent.environment.HERMES_HOME = "/var/lib/hermes/.hermes";
    };

  mkSystem =
    modules:
    nixpkgs.lib.nixosSystem {
      inherit system;
      modules = [
        self.nixosModules.default
        {
          boot.loader.grub.enable = false;
          fileSystems."/" = {
            device = "/dev/null";
            fsType = "ext4";
          };
          system.stateVersion = "25.11";
        }
      ]
      ++ modules;
    };

  accounts = {
    university = {
      provider = "microsoft";
      address = "student@example.edu";
      archiveFolder = "Archive/2026";
      notify = {
        mode = "triage";
        target = "discord:123";
        policyFile = pkgs.writeText "policy.md" "Notify for class mail.";
        markReadSilent = true;
      };
    };
    gmail = {
      provider = "google";
      address = "someone@gmail.com";
    };
  };

  plain = mkSystem [
    {
      services.hermes-mail = {
        enable = true;
        inherit accounts;
      };
    }
  ];
  withHermes = mkSystem [
    hermesStub
    {
      services.hermes-mail = {
        enable = true;
        inherit accounts;
        hermes.enable = true;
      };
    }
  ];
  unit = plain.config.systemd.services.hermes-mail;
  hermesUnits = withHermes.config.systemd.services;
in
{
  inherit plugin;
  hermes-mail = self.packages.${system}.hermes-mail;

  tests =
    pkgs.runCommand "hermes-mail-tests"
      {
        nativeBuildInputs = [
          pkgs.python314
          pkgs.python312
        ];
      }
      ''
        cp -r ${src} source
        chmod -R u+w source
        cd source
        python3.14 -W error::ResourceWarning -m unittest discover -s tests -v
        # The plugin and the client run on the Python of Hermes.
        python3.12 -m unittest tests.test_plugin -v
        touch $out
      '';

  lint = pkgs.runCommand "hermes-mail-lint" { nativeBuildInputs = [ pkgs.ruff ]; } ''
    cd ${src}
    ruff check --no-cache --select E,F,W,B --ignore E501 --target-version py312 .
    touch $out
  '';

  # The plugin package must hold only what Hermes loads.
  plugin-contents = pkgs.runCommand "hermes-mail-plugin-contents" { } ''
    test -f ${plugin}/plugin.yaml
    test -f ${plugin}/__init__.py
    test -f ${plugin}/skills/mail/SKILL.md
    test -f ${plugin}/hermes_mail/client.py
    test ! -e ${plugin}/hermes_mail/auth.py
    test ! -e ${plugin}/scripts
    test ! -e ${plugin}/tests
    touch $out
  '';

  module =
    assert unit.serviceConfig.User == "hermes-mail";
    assert
      unit.serviceConfig.RestrictAddressFamilies == [
        "AF_UNIX"
        "AF_INET"
        "AF_INET6"
      ];
    assert unit.serviceConfig.ProtectHome == true;
    assert plain.config.users.users.hermes-mail.isSystemUser;
    # No notifier and no Hermes changes without hermes.enable.
    assert !(plain.config.systemd.services ? hermes-mail-notify);
    assert hermesUnits.hermes-mail-notify.serviceConfig.User == "hermes";
    assert
      hermesUnits.hermes-mail-notify.serviceConfig.ExecStart
      == "${lib.getExe' pkgs.hello "hermes"} mail notify";
    assert hermesUnits.hermes-mail-notify.environment.HERMES_HOME == "/var/lib/hermes/.hermes";
    assert withHermes.config.services.hermes-agent.extraPlugins == [ plugin ];
    assert builtins.elem "hermes-mail" withHermes.config.users.users.hermes.extraGroups;
    pkgs.runCommand "hermes-mail-module"
      {
        nativeBuildInputs = [ pkgs.jq ];
        execStart = unit.serviceConfig.ExecStart;
        toplevel = builtins.unsafeDiscardStringContext plain.config.system.build.toplevel.drvPath;
      }
      ''
        config=$(echo "$execStart" | sed -E 's/.*--config ([^ ]+).*/\1/')
        jq -e '
          .socket == "/run/hermes-mail/mail.sock"
          and .accounts.university.notify.mode == "triage"
          and .accounts.university.notify.mark_read_silent
          and (.accounts.university.notify.policy_file | startswith("/nix/store/"))
          and .accounts.university.archive_folder == "Archive/2026"
          and .accounts.gmail.notify.mode == "none"
          and .accounts.gmail.sync_days == 7
          and .accounts.gmail.auth == null
          and .accounts.gmail.archive_folder == null
        ' "$config"
        echo "$toplevel" > $out
      '';

  formatting = pkgs.runCommand "nixfmt" { nativeBuildInputs = [ pkgs.nixfmt ]; } ''
    cd ${src}
    nixfmt --check flake.nix nix/*.nix nix/tests/*.nix
    touch $out
  '';
}
