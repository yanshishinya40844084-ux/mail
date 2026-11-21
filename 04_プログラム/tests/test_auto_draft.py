import email
import email.policy
import shutil
import sys
import tempfile
import unittest
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 04_プログラム
import auto_draft as ad  # noqa: E402

KANRI = "担当 <tantou@kanri.example.jp>"


def make_mail(subject, sender=KANRI, body="本文です。", msgid=None, charset="iso-2022-jp"):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "owner@yahoo.co.jp"
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = msgid or email.utils.make_msgid(domain="kanri.example.jp")
    m.set_content(body, charset=charset)
    return m.as_bytes()


class FakeIMAP:
    """auto_draft が使う IMAP 操作だけを真似した疑似サーバー."""

    def __init__(self, messages, flags=None, folders=None):
        self.messages = {str(i + 1).encode(): raw for i, raw in enumerate(messages)}
        self.flags = flags or {}
        self.folders = folders or [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Drafts) "/" "Draft"',
        ]
        self.appended = []
        self.readonly = None

    def list(self):
        return "OK", self.folders

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            uids = list(self.messages)
            if "FROM" in args:
                needle = args[args.index("FROM") + 1].strip('"').lower()
                uids = [u for u in uids if needle in self._headers(u).get("From", "").lower()]
            return "OK", [b" ".join(uids)]
        if command == "FETCH":
            uid, what = args
            raw = self.messages[uid]
            flags = self.flags.get(uid, "")
            if "HEADER.FIELDS" in what:
                payload = raw.split(b"\n\n", 1)[0] + b"\n\n"
            else:
                payload = raw
            meta = f"1 (UID {uid.decode()} FLAGS ({flags}) BODY[] {{{len(payload)}}}".encode()
            return "OK", [(meta, payload), b")"]
        raise AssertionError(command)

    def append(self, mailbox, flags, date, message):
        self.appended.append((mailbox, flags, message))
        return "OK", [b"APPEND completed"]

    def _headers(self, uid):
        return email.message_from_bytes(self.messages[uid], policy=email.policy.default)


def make_config(tmp):
    return ad.Config(
        accounts=[],
        rules=[
            ad.Rule("見積", "見積の定型文", ["@kanri.example.jp"], ["見積"]),
            ad.Rule("入退去", "入退去の定型文", ["@kanri.example.jp"], ["入居", "退去"]),
        ],
        lookback_days=7,
        quote_original=True,
        state_file=Path(tmp) / "processed.json",
    )


ACCOUNT = ad.Account(name="Yahoo", host="h", user="owner@yahoo.co.jp", password="x", from_name="坂本")


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = make_config(self.tmp.name)
        self.state = ad.State(self.config.state_file)

    def tearDown(self):
        self.tmp.cleanup()

    def run_mode(self, imap, mode="draft"):
        return ad.process_account(imap, ACCOUNT, self.config, self.state, mode, log=lambda *_: None)

    def drafts(self, imap):
        return [email.message_from_bytes(m, policy=email.policy.default) for _, _, m in imap.appended]

    def test_creates_drafts_for_matching_mail_only(self):
        imap = FakeIMAP([
            make_mail("【見積】101号室 給湯器交換"),
            make_mail("202号室 退去のご連絡"),
            make_mail("今月の収支報告"),                                  # ルールに一致しない
            make_mail("見積無料キャンペーン", sender="ad@spam.example"),   # 管理会社以外
        ])
        self.assertEqual(self.run_mode(imap), 2)
        self.assertTrue(imap.readonly, "受信箱は読み取り専用で開くこと")
        d1, d2 = self.drafts(imap)
        self.assertEqual(d1["Subject"], "Re: 【見積】101号室 給湯器交換")
        self.assertIn("見積の定型文", d1.get_content())
        self.assertEqual(d2["Subject"], "Re: 202号室 退去のご連絡")
        self.assertIn("入退去の定型文", d2.get_content())
        self.assertEqual(imap.appended[0][0], '"Draft"')
        self.assertIn("\\Draft", imap.appended[0][1])

    def test_reply_headers_and_quote(self):
        orig_id = "<abc@kanri.example.jp>"
        imap = FakeIMAP([make_mail("見積のご送付", body="お見積りを添付します。\nご確認ください。", msgid=orig_id)])
        self.run_mode(imap)
        (d,) = self.drafts(imap)
        self.assertEqual(d["To"], KANRI)
        self.assertEqual(d["In-Reply-To"], orig_id)
        self.assertIn(orig_id, d["References"])
        self.assertIn("坂本", d["From"])
        body = d.get_content()
        self.assertIn("> お見積りを添付します。", body)  # ISO-2022-JP の本文も読めている
        self.assertIn("> ご確認ください。", body)

    def test_no_duplicates_on_second_run(self):
        imap = FakeIMAP([make_mail("見積書")])
        self.assertEqual(self.run_mode(imap), 1)
        self.assertEqual(self.run_mode(imap), 0)
        reloaded = ad.State(self.config.state_file)
        self.assertEqual(len(reloaded.data["Yahoo"]), 1)

    def test_init_marks_without_drafting(self):
        imap = FakeIMAP([make_mail("見積書"), make_mail("入居のご報告")])
        self.assertEqual(self.run_mode(imap, "init"), 2)
        self.state.save()
        self.assertEqual(imap.appended, [])
        self.assertEqual(self.run_mode(imap), 0)

    def test_dry_run_saves_nothing(self):
        imap = FakeIMAP([make_mail("見積書")])
        self.assertEqual(self.run_mode(imap, "dry-run"), 1)
        self.assertEqual(imap.appended, [])
        self.assertFalse(self.config.state_file.exists())

    def test_skips_answered(self):
        imap = FakeIMAP([make_mail("見積書")], flags={b"1": "\\Seen \\Answered"})
        self.assertEqual(self.run_mode(imap), 0)

    def test_existing_re_prefix_kept(self):
        imap = FakeIMAP([make_mail("Re: 見積の件")])
        self.run_mode(imap)
        self.assertEqual(self.drafts(imap)[0]["Subject"], "Re: 見積の件")

    def test_gmail_japanese_drafts_folder(self):
        imap = FakeIMAP(
            [make_mail("見積書")],
            folders=[b'(\\HasNoChildren) "/" "INBOX"', b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/&Tgtm+DBN-"'],
        )
        self.run_mode(imap)
        self.assertEqual(imap.appended[0][0], '"[Gmail]/&Tgtm+DBN-"')


class HelperTests(unittest.TestCase):
    def test_utf7_roundtrip(self):
        self.assertEqual(ad.imap_utf7_encode("[Gmail]/下書き"), "[Gmail]/&Tgtm+DBN-")
        self.assertEqual(ad.imap_utf7_decode("[Gmail]/&Tgtm+DBN-"), "[Gmail]/下書き")
        self.assertEqual(ad.imap_utf7_decode(ad.imap_utf7_encode("A&B 送信済み")), "A&B 送信済み")

    def test_rule_exclude(self):
        r = ad.Rule("見積", "t", [], ["見積"], ["再見積依頼"])
        self.assertTrue(r.matches("x", "見積書送付"))
        self.assertFalse(r.matches("x", "再見積依頼"))

    def test_example_config_loads(self):
        """リポジトリの見本どおりのフォルダ構成で設定が読めること."""
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prog = tmp / "04_プログラム"
            prog.mkdir()
            shutil.copytree(repo / "02_返信テンプレート", tmp / "02_返信テンプレート")
            secrets = tmp / "secrets.toml"
            secrets.write_text('[Yahoo]\npassword = "pw"\n', encoding="utf-8")
            text = (repo / "04_プログラム" / "config.example.toml").read_text(encoding="utf-8")
            text = text.replace('"~/auto_draft_secrets.toml"', '"../secrets.toml"')
            (prog / "config.toml").write_text(text, encoding="utf-8")
            cfg = ad.load_config(prog / "config.toml")
            self.assertEqual(cfg.state_file, (tmp / "05_記録" / "processed.json").resolve())
        self.assertEqual([a.name for a in cfg.accounts], ["Yahoo"])  # Gmail は enabled = false
        self.assertEqual(cfg.accounts[0].password, "pw")
        self.assertEqual([r.name for r in cfg.rules], ["見積", "入退去"])
        self.assertTrue(cfg.rules[0].template.startswith("いつもお世話になります。坂本です。"))
        self.assertIn("見積書を確認し、承知しました。", cfg.rules[0].template)
        self.assertTrue(cfg.rules[1].template.endswith("引き続き宜しくお願いいたします！"))

    def test_missing_password_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text(
                '[settings]\nsecrets_file = "none.toml"\n'
                '[[accounts]]\nname = "Yahoo"\nhost = "h"\nuser = "u"\n'
                '[[rules]]\nname = "r"\ntemplate = "t"\n',
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as cm:
                ad.load_config(p)
        self.assertIn("[Yahoo]", str(cm.exception))

    def test_template_file_with_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "t.txt").write_bytes("\ufeff承知しました。\r\n".encode("utf-8"))
            p = Path(tmp) / "config.toml"
            p.write_text(
                '[[accounts]]\nname = "Y"\nhost = "h"\nuser = "u"\npassword = "x"\n'
                '[[rules]]\nname = "r"\ntemplate_file = "t.txt"\n',
                encoding="utf-8",
            )
            cfg = ad.load_config(p)
        self.assertEqual(cfg.rules[0].template.strip(), "承知しました。")

    def test_state_creates_record_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = ad.State(Path(tmp) / "05_記録" / "processed.json")
            st.add("Yahoo", "<id>", "2026-11-21 draft")
            st.save()
            self.assertTrue((Path(tmp) / "05_記録" / "processed.json").exists())

if __name__ == "__main__":
    unittest.main()
