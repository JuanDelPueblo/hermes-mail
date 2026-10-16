"""Unit tests for the protocol, MIME and config modules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from hermes_mail import config, imapproto, mime  # noqa: E402


class ParseFetchTest(unittest.TestCase):
    def test_literals_and_lists(self):
        data = [
            (b'1 (UID 7 FLAGS (\\Seen $Junk) BODY[HEADER.FIELDS (DATE SUBJECT)] {21}', b"Subject: Hello\r\n\r\n\r\n"),
            b' INTERNALDATE "07-Sep-2026 10:00:00 +0000" RFC822.SIZE 1200 BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 5 1 NIL NIL NIL NIL))',
            b'2 (UID 9 FLAGS ())',
        ]
        first, second = imapproto.parse_fetch(data)
        self.assertEqual(first["UID"], "7")
        self.assertEqual(first["FLAGS"], ["\\Seen", "$Junk"])
        self.assertEqual(imapproto.section(first, "BODY[HEADER"), b"Subject: Hello\r\n\r\n\r\n")
        self.assertEqual(first["INTERNALDATE"], "07-Sep-2026 10:00:00 +0000")
        self.assertEqual(first["RFC822.SIZE"], "1200")
        self.assertEqual(second, {"UID": "9", "FLAGS": []})

    def test_partial_section_key(self):
        response = imapproto.parse_fetch([(b"3 (UID 4 BODY[1.2]<0> {3}", b"abc"), b")"])[0]
        self.assertEqual(imapproto.section(response, "BODY[1.2]"), b"abc")
        self.assertIsNone(imapproto.section(response, "BODY[1]"))

    def test_quoted_strings_and_nil(self):
        response = imapproto.parse_fetch([b'1 (UID 1 X-TEST ("a \\"b\\"" NIL))'])[0]
        self.assertEqual(response["X-TEST"], ['a "b"', None])

    def test_unbalanced_list_is_an_error(self):
        with self.assertRaises(imapproto.ParseError):
            imapproto.parse_fetch([b"1 (UID 1 FLAGS (\\Seen)"])


class MailboxNameTest(unittest.TestCase):
    def test_modified_utf7_round_trip(self):
        for plain, encoded in (
            ("INBOX", "INBOX"),
            ("Programación", "Programaci&APM-n"),
            ("R&D", "R&-D"),
            ("日本語", "&ZeVnLIqe-"),
            ("a/é/b", "a/&AOk-/b"),
        ):
            self.assertEqual(imapproto.encode_mailbox(plain), encoded)
            self.assertEqual(imapproto.decode_mailbox(encoded), plain)

    def test_decode_keeps_a_name_that_is_not_valid(self):
        self.assertEqual(imapproto.decode_mailbox("a&!!-b"), "a&!!-b")

    def test_quote_encodes_the_name(self):
        self.assertEqual(imapproto.quote_mailbox('Clases/"Programación"'), '"Clases/\\"Programaci&APM-n\\""')


class ParseListTest(unittest.TestCase):
    def test_quoted_atom_and_literal_names(self):
        data = [
            b'(\\HasNoChildren) "/" INBOX',
            b'(\\HasNoChildren \\Sent) "." "Sent Items"',
            (b'(\\HasNoChildren) "/" {5}', b"Dr&-aft"),
            b"",
            b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
            b'(\\NonExistent) NIL "Gone"',
            b'() "/" "Say \\"hi\\""',
        ]
        folders = imapproto.parse_list(data)
        self.assertEqual([item["name"] for item in folders], ["INBOX", "Sent Items", "Dr&aft", 'Say "hi"'])
        self.assertEqual(folders[1]["flags"], ["\\HasNoChildren", "\\Sent"])

    def test_a_line_that_is_not_a_list_response_is_an_error(self):
        with self.assertRaises(imapproto.ParseError):
            imapproto.parse_list([b"nonsense"])


class BodyStructureTest(unittest.TestCase):
    def test_nested_multipart(self):
        structure = imapproto.parse_fetch([
            b'1 (BODYSTRUCTURE ((("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "QUOTED-PRINTABLE" 20 1 NIL NIL NIL NIL)'
            b'("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "BASE64" 400 6 NIL NIL NIL NIL) "ALTERNATIVE")'
            b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo@x>" NIL "BASE64" 800 NIL ("INLINE" ("FILENAME" "logo.png")) NIL NIL)'
            b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 9000 NIL ("ATTACHMENT" ("FILENAME*" "utf-8\'\'r%C3%BAbrica.pdf")) NIL NIL)'
            b'("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 300 ("date" "subj" NIL NIL NIL NIL NIL NIL NIL NIL) ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 10 1) 12 NIL NIL NIL NIL)'
            b' "MIXED"))'
        ])[0]["BODYSTRUCTURE"]
        parts = imapproto.parse_bodystructure(structure)
        self.assertEqual([p.number for p in parts], ["1.1", "1.2", "2", "3", "4"])
        plain, html = imapproto.text_parts(parts)
        self.assertEqual((plain.number, plain.encoding, plain.charset), ("1.1", "quoted-printable", "utf-8"))
        self.assertEqual(html.number, "1.2")
        attachments = imapproto.attachment_parts(parts)
        self.assertEqual([a.filename for a in attachments], ["logo.png", "rúbrica.pdf", ""])
        self.assertEqual(attachments[0].content_id, "logo@x")
        self.assertEqual(attachments[2].content_type, "message/rfc822")

    def test_single_part_is_part_one(self):
        structure = ["TEXT", "PLAIN", ["CHARSET", "us-ascii"], None, None, "7BIT", "12", "1"]
        (part,) = imapproto.parse_bodystructure(structure)
        self.assertEqual((part.number, part.content_type, part.is_attachment), ("1", "text/plain", False))

    def test_part_round_trip(self):
        part = imapproto.Part("2", "application/pdf", {"name": "a.pdf"}, "base64", 10, "", "attachment", {"filename": "a.pdf"})
        again = imapproto.Part.from_dict(part.to_dict())
        self.assertEqual((again.filename, again.is_attachment, again.encoding), ("a.pdf", True, "base64"))

    def test_helpers(self):
        self.assertEqual(imapproto.compact_set([9, 3, 4, 5, 7, 8]), "3:5,7:9")
        self.assertEqual(imapproto.quote_mailbox('Work "A"'), '"Work \\"A\\""')
        from datetime import date
        self.assertEqual(imapproto.imap_date(date(2026, 9, 7)), "07-Sep-2026")
        self.assertEqual(imapproto.decode_words("=?utf-8?q?caf=C3=A9?=.pdf"), "café.pdf")


class MimeTest(unittest.TestCase):
    def test_partial_base64(self):
        import base64
        encoded = base64.encodebytes("ñandú ".encode() * 50)
        text = mime.decode_text(mime.decode_transfer(encoded[:101], "base64", partial=True), "utf-8")
        self.assertTrue(text.startswith("ñandú ñandú"))

    def test_partial_quoted_printable(self):
        data = mime.decode_transfer(b"caf=C3=A9 =C3", "quoted-printable", partial=True)
        self.assertEqual(mime.decode_text(data, "utf-8", partial=True), "café ")

    def test_charset_fallback(self):
        self.assertEqual(mime.decode_text("canción".encode("latin-1"), "bogus-charset"), "canción")

    def test_choose_body(self):
        self.assertEqual(mime.choose_body("Plain text.", ""), "Plain text.")
        self.assertEqual(mime.choose_body("x", "Real content. " * 60), ("Real content. " * 60).strip())

    def test_headers(self):
        headers = mime.parse_headers(b"From: =?utf-8?q?Jos=C3=A9?= <j@x.edu>\r\nSubject: Re:\r\n =?utf-8?q?Tarea?=\r\n"
                                     b"Date: Mon, 07 Sep 2026 10:00:00 -0400\r\nMessage-ID: <a@b>\r\n\r\n")
        self.assertEqual(headers["sender"], "José <j@x.edu>")
        self.assertEqual(headers["subject"], "Re: Tarea")
        self.assertEqual(headers["message_id"], "<a@b>")
        self.assertEqual(headers["date"], "2026-09-07T10:00:00-04:00")

    def test_safe_filename(self):
        self.assertEqual(mime.safe_filename("../../etc/passwd", 0, "text/plain"), "passwd")
        self.assertEqual(mime.safe_filename("", 2, "application/pdf"), "attachment-2.pdf")


class ConfigTest(unittest.TestCase):
    def base(self, **account):
        return {"state_dir": "/var/lib/hermes-mail", "accounts": {"uni": {"provider": "microsoft", "address": "a@b.edu", **account}}}

    def test_defaults(self):
        cfg = config.parse(self.base())
        account = cfg.accounts["uni"]
        self.assertEqual((account.auth, account.folders, account.sync_days, account.notify.mode), ("oauth", ("INBOX",), 7, "none"))
        self.assertEqual(str(cfg.socket), "/run/hermes-mail/mail.sock")

    def test_archive_folder_defaults_by_provider(self):
        microsoft = config.parse(self.base()).accounts["uni"]
        self.assertEqual(microsoft.archive_folder, "Archive")
        google = config.parse(self.base(provider="google")).accounts["uni"]
        self.assertEqual(google.archive_folder, "[Gmail]/All Mail")
        imap = config.parse(self.base(provider="imap", host="mail.example.com", password_file="/x")).accounts["uni"]
        self.assertEqual(imap.archive_folder, "")
        overridden = config.parse(self.base(archive_folder="Old Mail")).accounts["uni"]
        self.assertEqual(overridden.archive_folder, "Old Mail")

    def test_notify_modes(self):
        for mode in ("triage", "agent"):
            notify = config.parse(self.base(notify={"mode": mode, "target": "discord:1"})).accounts["uni"].notify
            self.assertEqual(notify.mode, mode)
            self.assertNotIn("task_command", notify.to_dict())
        with self.assertRaisesRegex(config.ConfigError, "'all' was removed"):
            config.parse(self.base(notify={"mode": "all", "target": "discord:1"}))
        with self.assertRaisesRegex(config.ConfigError, "notify.target"):
            config.parse(self.base(notify={"mode": "agent"}))
        with self.assertRaisesRegex(config.ConfigError, "notify.mode must be one of"):
            config.parse(self.base(notify={"mode": "loud"}))

    def test_a_task_command_is_ignored_with_a_warning(self):
        with self.assertLogs("hermes_mail", "WARNING") as logs:
            notify = config.parse(self.base(notify={"mode": "triage", "target": "d:1", "task_command": ["/bin/task"]})).accounts["uni"].notify
        self.assertIn("task_command was removed", logs.output[0])
        self.assertEqual(notify.mode, "triage")

    def test_triage_retention(self):
        self.assertEqual(config.parse(self.base()).triage_retention_days, 30)
        self.assertEqual(config.parse({**self.base(), "triage_retention_days": 0}).triage_retention_days, 0)
        for value in (-1, 4000):
            with self.assertRaisesRegex(config.ConfigError, "triage_retention_days"):
                config.parse({**self.base(), "triage_retention_days": value})

    def test_errors(self):
        for account, message in [
            ({"provider": "yahoo"}, "provider"),
            ({"provider": "imap"}, "password"),
            ({"auth": "password"}, "password_file"),
            ({"notify": {"mode": "triage"}}, "notify.target"),
            ({"sync_days": 0}, "sync_days"),
        ]:
            with self.subTest(account=account), self.assertRaisesRegex(config.ConfigError, message):
                config.parse(self.base(**account))
        with self.assertRaisesRegex(config.ConfigError, "account name"):
            config.parse({"state_dir": "/x", "accounts": {"Bad.Name": {"provider": "microsoft", "address": "a"}}})


if __name__ == "__main__":
    unittest.main()
