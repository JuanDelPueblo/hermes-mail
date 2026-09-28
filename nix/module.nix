{
  config,
  lib,
  options,
  pkgs,
  ...
}:
let
  cfg = config.services.hermes-mail;
  inherit (lib) mkOption types;

  socketPath = "/run/hermes-mail/mail.sock";
  defaultUser = "hermes-mail";

  notifyOptions = {
    options = {
      mode = mkOption {
        type = types.enum [
          "triage"
          "all"
          "none"
        ];
        default = "none";
        description = ''
          What the notifier does with new mail of this account. `triage`
          classifies each message with the LLM and notifies only for the
          messages that the policy selects. `all` notifies for every message.
          `none` sends nothing, but the tools can still read the account.
        '';
      };
      target = mkOption {
        type = types.str;
        default = "";
        example = "discord:1234567890";
        description = "The Hermes send target for notifications, in the `hermes send --to` format.";
      };
      policyFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = "A text file with the triage rules for this account. The file goes into the Nix store.";
      };
      markReadSilent = mkOption {
        type = types.bool;
        default = false;
        description = "Mark mail as read on the server when the triage decides not to notify.";
      };
      taskCommand = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "/run/current-system/sw/bin/my-task-helper" ];
        description = ''
          A command that creates a task when the triage finds assigned work.
          The notifier runs it as the Hermes user with the task as JSON on
          stdin: title, due, list, notes, mail_id, message_id, subject,
          sender and account. The first line of its output goes into the
          notification.
        '';
      };
    };
  };

  accountOptions =
    { name, ... }:
    {
      options = {
        provider = mkOption {
          type = types.enum [
            "microsoft"
            "google"
            "imap"
          ];
          description = ''
            `microsoft` covers Microsoft 365 work or school accounts and
            personal Outlook.com accounts. `google` covers Gmail. Both sign in
            with OAuth2 and the public Thunderbird client. `imap` is any other
            server with a password.
          '';
        };
        address = mkOption {
          type = types.str;
          description = "The mail address, which is also the IMAP user name.";
        };
        auth = mkOption {
          type = types.nullOr (
            types.enum [
              "oauth"
              "password"
            ]
          );
          default = null;
          description = "The sign-in method. The default is `oauth`, and `password` for the `imap` provider.";
        };
        host = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "The IMAP host. The `microsoft` and `google` providers set it.";
        };
        port = mkOption {
          type = types.port;
          default = 993;
          description = "The IMAP port. The service always uses TLS.";
        };
        passwordFile = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = ''
            A file with the password, for example an app password. The service
            user must be able to read it. Use a path outside the Nix store,
            such as a sops-nix secret.
          '';
        };
        folders = mkOption {
          type = types.nonEmptyListOf types.str;
          default = [ "INBOX" ];
          description = "The IMAP folders to index. The service waits for new mail with IDLE on the first folder.";
        };
        archiveFolder = mkOption {
          type = types.nullOr types.str;
          default = null;
          example = "Archive";
          description = ''
            The folder `mail_archive`/`hermes-mail archive` moves messages to.
            The default is `Archive` for the `microsoft` provider and
            `[Gmail]/All Mail` for `google`. The `imap` provider has no
            default; set this to use archiving with it.
          '';
        };
        syncDays = mkOption {
          type = types.ints.between 1 365;
          default = 7;
          description = "The sync window. The service never reads mail that is older.";
        };
        pollSeconds = mkOption {
          type = types.ints.between 30 1500;
          default = 300;
          description = "The time between full checks of the sync window. IDLE finds new mail between checks.";
        };
        cacheLimitMB = mkOption {
          type = types.ints.positive;
          default = 100;
          description = "The size limit of the cached message text for this account.";
        };
        maxPartMB = mkOption {
          type = types.ints.positive;
          default = 25;
          description = "The largest attachment that the service downloads.";
        };
        notify = mkOption {
          type = types.submodule notifyOptions;
          default = { };
          description = "Notifications for new mail of the `${name}` account.";
        };
      };
    };

  serviceConfig = (pkgs.formats.json { }).generate "hermes-mail.json" {
    state_dir = cfg.stateDir;
    socket = socketPath;
    export_dir = cfg.exportDir;
    extract_root = cfg.extractRoot;
    accounts = lib.mapAttrs (_: account: {
      inherit (account)
        provider
        address
        auth
        host
        port
        folders
        ;
      password_file = account.passwordFile;
      archive_folder = account.archiveFolder;
      sync_days = account.syncDays;
      poll_seconds = account.pollSeconds;
      cache_limit_mb = account.cacheLimitMB;
      max_part_mb = account.maxPartMB;
      notify = {
        inherit (account.notify) mode target;
        policy_file = if account.notify.policyFile == null then null else "${account.notify.policyFile}";
        mark_read_silent = account.notify.markReadSilent;
        task_command = account.notify.taskCommand;
      };
    }) cfg.accounts;
  };

  writablePaths = [
    cfg.stateDir
    cfg.exportDir
  ]
  ++ lib.optional (cfg.extractRoot != null) cfg.extractRoot;
  needsHome = lib.any (
    path: lib.hasPrefix "/home/" path || lib.hasPrefix "/root/" path
  ) writablePaths;

  hasHermes = options ? services.hermes-agent;
  hermesCfg = config.services.hermes-agent;
in
{
  options.services.hermes-mail = {
    enable = lib.mkEnableOption "the hermes-mail IMAP service";

    package = mkOption {
      type = types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalMD "the hermes-mail package of this flake";
      description = "The package with `hermes-maild` and `hermes-mail`.";
    };

    pluginPackage = mkOption {
      type = types.package;
      default = pkgs.callPackage ./plugin.nix { };
      defaultText = lib.literalMD "the plugin package of this flake";
      description = "The Hermes plugin directory.";
    };

    user = mkOption {
      type = types.str;
      default = defaultUser;
      description = ''
        The user of the service. With the default, the module makes a system
        user. The Hermes user needs the service group to use the socket.
      '';
    };

    group = mkOption {
      type = types.str;
      default = defaultUser;
      description = "The group of the service. Members of this group can use the socket and read exported attachments.";
    };

    stateDir = mkOption {
      type = types.str;
      default = "/var/lib/hermes-mail";
      description = "The index, the message cache and the sign-in tokens. The tokens have mode 0600.";
    };

    exportDir = mkOption {
      type = types.str;
      default = "${cfg.stateDir}/exports";
      defaultText = lib.literalExpression ''"''${config.services.hermes-mail.stateDir}/exports"'';
      description = "Where `export-attachment` saves files. The Hermes user must be able to read them.";
    };

    extractRoot = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "/home/alice";
      description = "`extract-attachment` writes only below this directory. Null turns the command off.";
    };

    socketPath = mkOption {
      type = types.str;
      default = socketPath;
      readOnly = true;
      description = "The service socket.";
    };

    logLevel = mkOption {
      type = types.enum [
        "DEBUG"
        "INFO"
        "WARNING"
        "ERROR"
      ];
      default = "INFO";
      description = "The log level of the service.";
    };

    accounts = mkOption {
      type = types.attrsOf (types.submodule accountOptions);
      default = { };
      description = "The mail accounts. An account name has lower-case letters, digits, `-` and `_`.";
    };

    hermes = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Connect the service to `services.hermes-agent`: add the plugin and
          the `hermes-mail` command to Hermes. Also enable the plugin in the
          Hermes config: `hermes plugins enable hermes-mail`.
        '';
      };
      notifier = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Run `hermes mail notify` as the Hermes user. It triages new mail and
          sends notifications. Configure its model with
          `auxiliary.hermes_mail_triage` in the Hermes settings.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        assertions = [
          {
            assertion = lib.all (name: builtins.match "[a-z][a-z0-9_-]{0,31}" name != null) (
              lib.attrNames cfg.accounts
            );
            message = "services.hermes-mail.accounts: a name must have 1 to 32 lower-case letters, digits, - or _, and start with a letter.";
          }
          {
            assertion = !cfg.hermes.enable || hasHermes;
            message = "services.hermes-mail.hermes.enable needs the services.hermes-agent NixOS module.";
          }
        ]
        ++ lib.concatLists (
          lib.mapAttrsToList (name: account: [
            {
              assertion = account.provider != "imap" || (account.host != null && account.passwordFile != null);
              message = "services.hermes-mail.accounts.${name}: the imap provider needs host and passwordFile.";
            }
            {
              assertion = account.auth != "password" || account.passwordFile != null;
              message = "services.hermes-mail.accounts.${name}: password sign-in needs passwordFile.";
            }
            {
              assertion = account.notify.mode == "none" || account.notify.target != "";
              message = "services.hermes-mail.accounts.${name}.notify.target is required when notify.mode is ${account.notify.mode}.";
            }
          ]) cfg.accounts
        );

        users.users = lib.mkIf (cfg.user == defaultUser) {
          ${defaultUser} = {
            isSystemUser = true;
            inherit (cfg) group;
            home = cfg.stateDir;
          };
        };
        users.groups = lib.mkIf (cfg.group == defaultUser) { ${defaultUser} = { }; };

        environment.systemPackages = [ cfg.package ];

        systemd.tmpfiles.settings.hermes-mail = lib.genAttrs [ cfg.stateDir cfg.exportDir ] (_: {
          d = {
            inherit (cfg) user group;
            mode = "0750";
          };
        });

        systemd.services.hermes-mail = {
          description = "hermes-mail IMAP index service";
          wantedBy = [ "multi-user.target" ];
          wants = [ "network-online.target" ];
          after = [ "network-online.target" ];
          unitConfig.RequiresMountsFor = writablePaths;
          serviceConfig = {
            ExecStart = "${lib.getExe' cfg.package "hermes-maild"} --config ${serviceConfig} --log-level ${cfg.logLevel}";
            User = cfg.user;
            Group = cfg.group;
            RuntimeDirectory = "hermes-mail";
            RuntimeDirectoryMode = "0750";
            Restart = "on-failure";
            RestartSec = 10;
            UMask = "0027";
            ReadWritePaths = writablePaths;
            NoNewPrivileges = true;
            ProtectSystem = "strict";
            ProtectHome = if needsHome then "read-only" else true;
            PrivateTmp = true;
            PrivateDevices = true;
            ProtectKernelTunables = true;
            ProtectKernelModules = true;
            ProtectKernelLogs = true;
            ProtectControlGroups = true;
            ProtectClock = true;
            ProtectHostname = true;
            ProtectProc = "invisible";
            ProcSubset = "pid";
            RestrictAddressFamilies = [
              "AF_UNIX"
              "AF_INET"
              "AF_INET6"
            ];
            RestrictNamespaces = true;
            RestrictRealtime = true;
            RestrictSUIDSGID = true;
            LockPersonality = true;
            CapabilityBoundingSet = "";
            SystemCallArchitectures = "native";
            SystemCallFilter = [
              "@system-service"
              "~@privileged"
              "~@resources"
            ];
          };
        };
      }

      (lib.optionalAttrs hasHermes {
        services.hermes-agent = lib.mkIf cfg.hermes.enable {
          extraPlugins = [ cfg.pluginPackage ];
          extraPackages = [ cfg.package ];
        };

        users.users = lib.mkIf (cfg.hermes.enable && hermesCfg.group != cfg.group) {
          ${hermesCfg.user}.extraGroups = [ cfg.group ];
        };

        systemd.services.hermes-mail-notify = lib.mkIf (cfg.hermes.enable && cfg.hermes.notifier) {
          description = "hermes-mail notifier: new-mail triage and notifications";
          wantedBy = [ "multi-user.target" ];
          wants = [ "hermes-mail.service" ];
          after = [
            "hermes-mail.service"
            "hermes-agent.service"
          ];
          # The same identity and environment as the Hermes gateway, so the
          # notifier reads the same config, credentials and plugins.
          # PATH gets mkForce, because NixOS already defines a default PATH
          # for every unit and the two definitions would conflict.
          environment = lib.mapAttrs (
            name: value: if name == "PATH" then lib.mkForce value else value
          ) config.systemd.services.hermes-agent.environment;
          serviceConfig = {
            ExecStart = "${lib.getExe' hermesCfg.package "hermes"} mail notify";
            User = hermesCfg.user;
            Group = hermesCfg.group;
            WorkingDirectory = hermesCfg.stateDir;
            Restart = "always";
            RestartSec = 15;
          };
        };
      })
    ]
  );
}
