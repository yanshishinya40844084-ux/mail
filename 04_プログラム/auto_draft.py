#!/usr/bin/env python3
"""定型メールの返信下書きを自動作成するツール.

IMAP で受信箱を読み取り、設定したルール（送信者・件名のキーワード）に
一致したメールに対して、定型文の返信を「下書き」フォルダに保存します。
メールを送信することは一切ありません。送信は必ずご自身で確認して行います。

使い方:
    python auto_draft.py                 # 下書きを作成
    python auto_draft.py --dry-run       # 何が作られるか表示だけ（保存しない）
    python auto_draft.py --init          # 既存メールを「処理済み」にするだけ（初回用）
    python auto_draft.py --list-folders  # フォルダ名の一覧を表示（下書きフォルダ確認用）

Python 3.11 以上が必要です（追加のライブラリは不要）。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.toml"

HEADER_FIELDS = "FROM REPLY-TO SUBJECT MESSAGE-ID DATE REFERENCES"


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    name: str
    template: str
    from_contains: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    subject_excludes: list[str] = field(default_factory=list)

    def matches(self, from_header: str, subject: str) -> bool:
        f = from_header.lower()
        if self.from_contains and not any(k.lower() in f for k in self.from_contains):
            return False
        if self.subject_contains and not any(k in subject for k in self.subject_contains):
            return False
        if any(k in subject for k in self.subject_excludes):
            return False
        return True


@dataclass
class Account:
    name: str
    host: str
    user: str
    password: str
    port: int = 993
    inbox: str = "INBOX"
    drafts_folder: str = ""
    from_name: str = ""
    rules: list[str] = field(default_factory=list)  # 空ならすべてのルール


@dataclass
class Config:
    accounts: list[Account]
    rules: list[Rule]
    lookback_days: int = 7
    quote_original: bool = True
    state_file: Path = HERE / "processed.json"


def load_config(path: Path) -> Config:
    with open(path, "rb") as fp:
        raw = tomllib.load(fp)

    settings = raw.get("settings", {})
    base = path.resolve().parent

    def resolve(p: str) -> Path:
        q = Path(p).expanduser()
        return (q if q.is_absolute() else base / q).resolve()

    rules = []
    for r in raw.get("rules", []):
        if r.get("template_file"):
            tfile = resolve(r["template_file"])
            if not tfile.exists():
                raise SystemExit(f"設定エラー: ルール「{r['name']}」の返信文ファイルがありません: {tfile}")
            # utf-8-sig: Windows のメモ帳で保存した BOM 付きファイルにも対応
            template = tfile.read_text(encoding="utf-8-sig").strip("\n")
        elif r.get("template"):
            template = r["template"].strip("\n")
        else:
            raise SystemExit(f"設定エラー: ルール「{r['name']}」に template か template_file を書いてください。")
        rules.append(
            Rule(
                name=r["name"],
                template=template,
                from_contains=list(r.get("from_contains", [])),
                subject_contains=list(r.get("subject_contains", [])),
                subject_excludes=list(r.get("subject_excludes", [])),
            )
        )
    if not rules:
        raise SystemExit("設定エラー: [[rules]] が1つもありません。")

    secrets_path = resolve(settings.get("secrets_file", "~/auto_draft_secrets.toml"))
    secrets: dict = {}
    if secrets_path.exists():
        with open(secrets_path, "rb") as fp:
            secrets = tomllib.load(fp)

    accounts = []
    for a in raw.get("accounts", []):
        if a.get("enabled", True) is False:
            continue
        # パスワードの探し方: config の password → 環境変数 → パスワードファイル
        password = a.get("password", "")
        if not password and a.get("password_env"):
            password = os.environ.get(a["password_env"], "")
        if not password:
            password = secrets.get(a["name"], {}).get("password", "")
        if not password:
            raise SystemExit(
                f"設定エラー: アカウント「{a['name']}」のパスワードがありません。\n"
                f"パスワードファイル {secrets_path} に次のように書いてください:\n"
                f"[{a['name']}]\npassword = \"ここにパスワード\""
            )
        accounts.append(
            Account(
                name=a["name"],
                host=a["host"],
                port=int(a.get("port", 993)),
                user=a["user"],
                password=password,
                inbox=a.get("inbox", "INBOX"),
                drafts_folder=a.get("drafts_folder", ""),
                from_name=a.get("from_name", ""),
                rules=list(a.get("rules", [])),
            )
        )
    if not accounts:
        raise SystemExit("設定エラー: 有効な [[accounts]] が1つもありません。")

    state = resolve(settings.get("state_file", "processed.json"))

    return Config(
        accounts=accounts,
        rules=rules,
        lookback_days=int(settings.get("lookback_days", 7)),
        quote_original=bool(settings.get("quote_original", True)),
        state_file=state,
    )


# ---------------------------------------------------------------------------
# 処理済みの記録
# ---------------------------------------------------------------------------


class State:
    """下書きを作成済みのメールを記録し、二重に作らないようにする."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, str]] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def seen(self, account: str, key: str) -> bool:
        return key in self.data.get(account, {})

    def add(self, account: str, key: str, note: str) -> None:
        self.data.setdefault(account, {})[key] = note

    def prune(self, keep_days: int) -> None:
        cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
        for acc in self.data.values():
            for k in [k for k, v in acc.items() if v[:10] < cutoff]:
                del acc[k]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# IMAP の補助関数
# ---------------------------------------------------------------------------


def imap_utf7_encode(s: str) -> str:
    """フォルダ名を IMAP 用の修正 UTF-7 に変換する（例: 下書き）."""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(s: str) -> str:
    def repl(m: re.Match) -> str:
        body = m.group(1)
        if body == "":
            return "&"
        b64 = body.replace(",", "/")
        b64 += "=" * (-len(b64) % 4)
        return base64.b64decode(b64).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", repl, s)


LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')


def list_folders(imap: imaplib.IMAP4) -> list[tuple[str, str]]:
    """(フラグ, フォルダ名[修正UTF-7のまま]) のリストを返す."""
    typ, data = imap.list()
    folders = []
    for line in data or []:
        if not isinstance(line, bytes):
            continue
        m = LIST_RE.match(line)
        if not m:
            continue
        name = m.group("name").decode()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"')
        folders.append((m.group("flags").decode(), name))
    return folders


def quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_drafts_folder(imap: imaplib.IMAP4, configured: str) -> str:
    if configured:
        # 日本語で書かれていたら修正 UTF-7 に変換する
        return configured if configured.isascii() else imap_utf7_encode(configured)
    folders = list_folders(imap)
    for flags, name in folders:
        if "\\Drafts" in flags:
            return name
    for candidate in ("Draft", "Drafts", "下書き", "[Gmail]/Drafts", "[Gmail]/下書き"):
        enc = imap_utf7_encode(candidate)
        if any(name == enc for _, name in folders):
            return enc
    raise SystemExit(
        "下書きフォルダが見つかりません。`python auto_draft.py --list-folders` で"
        "フォルダ名を確認し、config.toml の drafts_folder に設定してください。"
    )


def imap_date(d: dt.date) -> str:
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def search_uids(imap: imaplib.IMAP4, since: dt.date, senders: list[str] | None) -> list[bytes]:
    """SINCE で検索。全ルールに ASCII の送信者条件があれば送信者でも絞り込む."""
    base = ["SINCE", imap_date(since)]
    uids: set[bytes] = set()
    if senders:
        for s in senders:
            typ, data = imap.uid("SEARCH", None, *base, "FROM", quote_mailbox(s))
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
    else:
        typ, data = imap.uid("SEARCH", None, *base)
        if typ == "OK" and data and data[0]:
            uids.update(data[0].split())
    return sorted(uids, key=int)


def fetch_one(imap: imaplib.IMAP4, uid: bytes, what: str) -> tuple[bytes, bytes]:
    """1通分を取得し、(メタ情報, 本体) を返す."""
    typ, data = imap.uid("FETCH", uid, f"(FLAGS {what})")
    if typ != "OK" or not data:
        return b"", b""
    meta, body = b"", b""
    for item in data:
        if isinstance(item, tuple):
            meta += item[0]
            body = item[1]
        elif isinstance(item, bytes):
            meta += item
    return meta, body


# ---------------------------------------------------------------------------
# 返信メールの作成
# ---------------------------------------------------------------------------


def text_of(msg: email.message.Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = re.sub(r"(?is)<(script|style).*?</\1>", "", content)
        content = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", content)
        content = html.unescape(re.sub(r"<[^>]+>", "", content))
    return content.replace("\r\n", "\n").strip()


def build_reply(
    original: email.message.Message,
    template: str,
    from_addr: str,
    from_name: str,
    quote: bool,
) -> EmailMessage:
    reply = EmailMessage()
    to = original.get("Reply-To") or original.get("From", "")
    subject = str(original.get("Subject", "")).strip()
    if not re.match(r"(?i)^(re|ｒｅ|返信)\s*[:：]", subject):
        subject = "Re: " + subject

    reply["From"] = email.utils.formataddr((from_name, from_addr)) if from_name else from_addr
    reply["To"] = str(to)
    reply["Subject"] = subject
    reply["Date"] = email.utils.formatdate(localtime=True)
    reply["Message-ID"] = email.utils.make_msgid(domain=from_addr.split("@")[-1] or None)

    orig_id = original.get("Message-ID")
    if orig_id:
        reply["In-Reply-To"] = str(orig_id)
        refs = str(original.get("References", "")).split()
        reply["References"] = " ".join(refs + [str(orig_id)])

    body = template.rstrip() + "\n"
    if quote:
        when = original.get("Date", "")
        who = original.get("From", "")
        quoted = "\n".join("> " + line for line in text_of(original).splitlines())
        body += f"\n\n{when} {who}:\n{quoted}\n"
    reply.set_content(body, charset="utf-8")
    return reply


def message_key(headers: email.message.Message) -> str:
    mid = str(headers.get("Message-ID", "")).strip()
    if mid:
        return mid
    raw = f"{headers.get('From')}|{headers.get('Date')}|{headers.get('Subject')}"
    return "sha1:" + hashlib.sha1(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------


def connect(account: Account) -> imaplib.IMAP4:
    imap = imaplib.IMAP4_SSL(account.host, account.port)
    imap.login(account.user, account.password)
    return imap


def ascii_senders(rules: list[Rule]) -> list[str] | None:
    """全ルールに ASCII の送信者条件があるときだけ、その一覧を返す."""
    if not rules or any(not r.from_contains for r in rules):
        return None
    senders = sorted({s for r in rules for s in r.from_contains})
    return senders if all(s.isascii() for s in senders) else None


def process_account(
    imap: imaplib.IMAP4,
    account: Account,
    config: Config,
    state: State,
    mode: str,
    log=print,
) -> int:
    """1アカウント分を処理し、作成した（作成予定の）下書き数を返す."""
    rules = [r for r in config.rules if not account.rules or r.name in account.rules]
    since = dt.date.today() - dt.timedelta(days=config.lookback_days)

    drafts = find_drafts_folder(imap, account.drafts_folder) if mode == "draft" else ""
    typ, _ = imap.select(quote_mailbox(account.inbox), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"受信箱「{account.inbox}」を開けませんでした")

    count = 0
    for uid in search_uids(imap, since, ascii_senders(rules)):
        meta, raw_headers = fetch_one(imap, uid, f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]")
        if not raw_headers:
            continue
        headers = email.message_from_bytes(raw_headers, policy=email.policy.default)
        key = message_key(headers)
        if state.seen(account.name, key):
            continue
        if b"\\Answered" in meta:  # 返信済み
            continue

        from_h = str(headers.get("From", ""))
        subject = str(headers.get("Subject", ""))
        rule = next((r for r in rules if r.matches(from_h, subject)), None)
        if rule is None:
            continue

        today = dt.date.today().isoformat()
        label = f"[{account.name}] {rule.name}: {subject}  ({from_h})"
        if mode == "init":
            state.add(account.name, key, f"{today} init {rule.name}")
            log(f"処理済みに登録  {label}")
            count += 1
            continue

        _, raw = fetch_one(imap, uid, "BODY.PEEK[]")
        if not raw:
            continue
        original = email.message_from_bytes(raw, policy=email.policy.default)
        reply = build_reply(original, rule.template, account.user, account.from_name, config.quote_original)

        if mode == "dry-run":
            log(f"[作成予定] {label}")
            log("    宛先: " + reply["To"] + " / 件名: " + reply["Subject"])
            for line in rule.template.splitlines():
                log("    | " + line)
        else:
            typ, resp = imap.append(
                quote_mailbox(drafts),
                "(\\Draft \\Seen)",
                imaplib.Time2Internaldate(time.time()),
                reply.as_bytes(policy=email.policy.SMTP),
            )
            if typ != "OK":
                raise RuntimeError(f"下書きの保存に失敗しました: {resp!r}")
            state.add(account.name, key, f"{today} draft {rule.name}")
            state.save()  # 途中で止まっても二重作成しないよう、1通ごとに保存
            log(f"下書きを作成  {label}")
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="定型メールの返信下書きを自動作成します（送信はしません）")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="設定ファイル（既定: config.toml）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="保存せず、作成予定の下書きを表示するだけ")
    g.add_argument("--init", action="store_true", help="今あるメールを処理済みとして記録する（初回用）")
    g.add_argument("--list-folders", action="store_true", help="IMAP のフォルダ一覧を表示する")
    args = p.parse_args(argv)

    if not args.config.exists():
        print(f"設定ファイルがありません: {args.config}\n"
              "config.example.toml を config.toml にコピーして編集してください。", file=sys.stderr)
        return 2
    config = load_config(args.config)
    state = State(config.state_file)
    mode = "dry-run" if args.dry_run else "init" if args.init else "draft"

    failed = False
    for account in config.accounts:
        try:
            imap = connect(account)
        except (imaplib.IMAP4.error, OSError) as e:
            print(f"[{account.name}] ログインできませんでした: {e}", file=sys.stderr)
            failed = True
            continue
        try:
            if args.list_folders:
                print(f"[{account.name}] のフォルダ:")
                for flags, name in list_folders(imap):
                    print(f"  {imap_utf7_decode(name)}    {flags}")
                continue
            n = process_account(imap, account, config, state, mode)
            print(f"[{account.name}] {n} 件" + ("（表示のみ）" if mode == "dry-run" else ""))
        except Exception as e:  # 1アカウントの失敗で他を止めない
            print(f"[{account.name}] エラー: {e}", file=sys.stderr)
            failed = True
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    if mode != "dry-run" and not args.list_folders:
        state.prune(keep_days=max(config.lookback_days * 4, 60))
        state.save()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
