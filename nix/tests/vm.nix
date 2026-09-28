{ pkgs, self }:
let
  # A throwaway certificate for the test server only.
  cert = pkgs.runCommand "hermes-mail-test-cert" { nativeBuildInputs = [ pkgs.openssl ]; } ''
    mkdir $out
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=localhost \
      -addext subjectAltName=DNS:localhost -keyout $out/key.pem -out $out/cert.pem
  '';
  mail = ./mail.eml;
in
pkgs.testers.runNixOSTest {
  name = "hermes-mail";

  nodes.machine =
    { config, ... }:
    {
      imports = [ self.nixosModules.default ];

      security.pki.certificateFiles = [ "${cert}/cert.pem" ];

      users.users.alice = {
        isNormalUser = true;
        password = "alice-password";
      };

      services.dovecot2 = {
        enable = true;
        enablePAM = true;
        settings = {
          dovecot_config_version = config.services.dovecot2.package.version;
          dovecot_storage_version = config.services.dovecot2.package.version;
          protocols = [ "imap" ];
          mail_driver = "maildir";
          mail_path = "~/Maildir";
          ssl_server_cert_file = "${cert}/cert.pem";
          ssl_server_key_file = "${cert}/key.pem";
        };
      };

      environment.etc."hermes-mail-password".text = "alice-password";
      environment.systemPackages = [ pkgs.dovecot ];

      services.hermes-mail = {
        enable = true;
        accounts.local = {
          provider = "imap";
          host = "localhost";
          address = "alice";
          passwordFile = "/etc/hermes-mail-password";
          pollSeconds = 30;
        };
      };
    };

  testScript = ''
    import json

    def cli(command):
        return json.loads(machine.succeed(f"hermes-mail {command}"))

    def deliver():
        machine.succeed("doveadm save -u alice < ${mail}")

    machine.wait_for_unit("dovecot.service")
    machine.wait_for_unit("hermes-mail.service")

    # The first sync of the empty mailbox is the baseline.
    machine.wait_until_succeeds("hermes-mail status | grep -q '\"status\": \"idle\"'", timeout=60)

    with subtest("new mail makes one event"):
        deliver()
        machine.wait_until_succeeds("hermes-mail events | grep -q mail.new", timeout=60)
        events = cli("events")["events"]
        assert len(events) == 1, events
        mail_id = events[0]["mail_id"]

    with subtest("list, show and attachments"):
        listed = cli("list")
        assert listed["count"] == 1, listed
        assert listed["messages"][0]["subject"] == "Lab 3 rubric", listed
        shown = cli(f"show {mail_id}")
        assert "rubric for lab 3" in shown["body"], shown
        assert shown["attachments"][0]["name"] == "rubric.txt", shown
        exported = cli(f"export-attachment {mail_id} 0")
        content = machine.succeed(f"cat {exported['path']}")
        assert "Criteria" in content, content

    with subtest("reading does not mark read"):
        flags = machine.succeed("doveadm fetch -u alice flags mailbox INBOX all")
        assert "\\Seen" not in flags, flags

    with subtest("mark read and unread on the server"):
        cli(f"mark-read {mail_id}")
        machine.succeed("doveadm fetch -u alice flags mailbox INBOX all | grep -q Seen")
        cli(f"mark-unread {mail_id}")
        machine.fail("doveadm fetch -u alice flags mailbox INBOX all | grep -q Seen")

    with subtest("the socket and the state are private"):
        machine.succeed("test \"$(stat -c %a /run/hermes-mail/mail.sock)\" = 660")
        machine.succeed("test \"$(stat -c %U /var/lib/hermes-mail/index.sqlite3)\" = hermes-mail")
        machine.fail("sudo -u alice hermes-mail status")

    with subtest("the service survives a restart without new events"):
        machine.succeed("systemctl restart hermes-mail.service")
        machine.wait_until_succeeds("hermes-mail status | grep -q '\"status\": \"idle\"'", timeout=60)
        assert len(cli("events")["events"]) == 1
  '';
}
