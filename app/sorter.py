"""Раскладка писем по папкам на IMAP-сервере согласно правилам."""
import email
import email.header
import email.utils
import imaplib
import logging
import re

from . import imap_utf7
from .config import Rule

log = logging.getLogger("sorter")
LIST_RE = re.compile(rb'\((?P<flags>.*?)\) "(?P<sep>.)" (?P<name>.*)')


def _decode_header(value: str) -> str:
    parts = []
    for text, enc in email.header.decode_header(value or ""):
        if isinstance(text, bytes):
            try:
                text = text.decode(enc or "utf-8", errors="replace")
            except LookupError:
                text = text.decode("utf-8", errors="replace")
        parts.append(text)
    return "".join(parts)


def _quote(raw_name: str) -> str:
    return '"' + raw_name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MailboxSorter:
    def __init__(self, host, port, login, password, rules, skip_folders, timeout=120):
        self.host, self.port = host, port
        self.login, self.password = login, password
        self.rules: list[Rule] = rules
        self.skip = set(skip_folders) | {r.move_to for r in rules}
        self.timeout = timeout

    # ---------- IMAP helpers ----------
    def _connect(self):
        m = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        m.login(self.login, self.password)
        typ, caps = m.capability()
        self.caps = set(caps[0].decode().upper().split()) if typ == "OK" and caps else set()
        return m

    @staticmethod
    def _folders(m):
        typ, data = m.list()
        res = {}
        for line in data or []:
            if not line:
                continue
            mt = LIST_RE.match(line)
            if not mt or b"\\Noselect" in mt.group("flags"):
                continue
            raw = mt.group("name").decode().strip().strip('"')
            res[imap_utf7.decode(raw)] = raw
        return res

    def _ensure_folder(self, m, folders, name):
        if name in folders:
            return folders[name]
        raw = imap_utf7.encode(name)
        typ, _ = m.create(_quote(raw))
        if typ != "OK":
            raise RuntimeError(f"не удалось создать папку {name}")
        m.subscribe(_quote(raw))
        folders[name] = raw
        log.info("[%s] создана папка %s", self.login, name)
        return raw

    def _search(self, m):
        """UID писем-кандидатов в выбранной папке (грубый поиск на сервере)."""
        uids = set()
        for rule in self.rules:
            for addr in sorted(rule.senders):
                typ, d = m.uid("SEARCH", None, "FROM", _quote(addr))
                if typ == "OK" and d and d[0]:
                    uids.update(d[0].split())
            for dom in sorted(rule.sender_domains):
                typ, d = m.uid("SEARCH", None, "FROM", _quote("@" + dom))
                if typ == "OK" and d and d[0]:
                    uids.update(d[0].split())
            for s in rule.subject_contains:
                m.literal = s.encode("utf-8")
                typ, d = m.uid("SEARCH", "CHARSET", "UTF-8", "SUBJECT")
                if typ == "OK" and d and d[0]:
                    uids.update(d[0].split())
        return sorted(uids, key=int)

    def _fetch_headers(self, m, uids):
        out = {}
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i:i + 200]).decode()
            typ, data = m.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            for item in data or []:
                if not isinstance(item, tuple):
                    continue
                uid = re.search(rb"UID (\d+)", item[0])
                if not uid:
                    continue
                msg = email.message_from_bytes(item[1])
                sender = email.utils.parseaddr(_decode_header(msg.get("From", "")))[1].lower()
                out[uid.group(1)] = (sender, _decode_header(msg.get("Subject", "")))
        return out

    def _move(self, m, uids, dest_raw, mark_read):
        uid_set = b",".join(uids).decode()
        if mark_read:
            m.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Seen)")
        if "MOVE" in self.caps:
            typ, d = m.uid("MOVE", uid_set, _quote(dest_raw))
            if typ != "OK":
                raise RuntimeError(f"MOVE не прошёл: {d}")
            return
        typ, d = m.uid("COPY", uid_set, _quote(dest_raw))
        if typ != "OK":
            raise RuntimeError(f"COPY не прошёл: {d}")
        m.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
        if "UIDPLUS" in self.caps:
            m.uid("EXPUNGE", uid_set)
        else:
            m.expunge()

    # ---------- main ----------
    def run_once(self) -> int:
        """Один проход по всем папкам ящика. Возвращает число перенесённых писем."""
        if not self.rules:
            return 0
        moved_total = 0
        m = self._connect()
        try:
            folders = self._folders(m)
            dest = {r.move_to: self._ensure_folder(m, folders, r.move_to) for r in self.rules}
            for name, raw in list(folders.items()):
                if name in self.skip:
                    continue
                typ, _ = m.select(_quote(raw))
                if typ != "OK":
                    log.warning("[%s] не открылась папка %s", self.login, name)
                    continue
                uids = self._search(m)
                if not uids:
                    continue
                headers = self._fetch_headers(m, uids)
                plan = {}
                for uid, (sender, subject) in headers.items():
                    for rule in self.rules:
                        if rule.matches(sender, subject):
                            plan.setdefault((rule.move_to, rule.mark_read, rule.name), []).append(uid)
                            break
                for (move_to, mark_read, rule_name), muids in plan.items():
                    self._move(m, muids, dest[move_to], mark_read)
                    moved_total += len(muids)
                    log.info("[%s] %s: %s -> %s, писем %d", self.login, rule_name, name, move_to, len(muids))
                m.close()
        finally:
            try:
                m.logout()
            except Exception:
                pass
        return moved_total
