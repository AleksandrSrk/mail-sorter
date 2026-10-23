"""Раскладка писем по папкам на IMAP-сервере согласно правилам."""
import email
import email.header
import email.utils
import imaplib
import logging
import re

from . import imap_utf7
from .config import LearnSpam, Rule

log = logging.getLogger("sorter")
LIST_RE = re.compile(rb'\((?P<flags>.*?)\) "(?P<sep>.)" (?P<name>.*)')
STATUS_RE = re.compile(rb"UIDNEXT (\d+).*?UIDVALIDITY (\d+)|UIDVALIDITY (\d+).*?UIDNEXT (\d+)")
HEADERS = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT X-SPAM-FLAG X-SPAM-STATUS)])"
CHUNK = 500


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


def parse_status(line: bytes):
    """-> (uidnext, uidvalidity) из ответа STATUS."""
    m = STATUS_RE.search(line or b"")
    if not m:
        return None, None
    if m.group(1):
        return int(m.group(1)), int(m.group(2))
    return int(m.group(4)), int(m.group(3))


class MailboxSorter:
    def __init__(self, host, port, login, password, rules, skip_folders, box_state: dict,
                 learn: LearnSpam | None = None, learned=None, timeout=120):
        self.host, self.port = host, port
        self.login, self.password = login, password
        self.rules: list[Rule] = rules
        self.skip = set(skip_folders) | {r.move_to for r in rules}
        self.state = box_state            # {raw_folder: [uidvalidity, last_uid]}
        self.learn = learn
        self.learned = learned            # LearnedSpam
        self.timeout = timeout
        self.caps = set()

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

    def _new_uids(self, m, raw, full):
        """Выбирает папку и возвращает (uids к проверке, uidvalidity, max_uid). None — папку пропустить."""
        prev = self.state.get(raw)
        typ, st = m.status(_quote(raw), "(UIDNEXT UIDVALIDITY)")
        uidnext, uidvalidity = parse_status(st[0] if typ == "OK" and st else b"")
        if uidnext is None:
            full = True
        elif not full and prev and prev[0] == uidvalidity and uidnext - 1 <= prev[1]:
            return None  # ничего нового
        typ, _ = m.select(_quote(raw))
        if typ != "OK":
            log.warning("[%s] не открылась папка %s", self.login, imap_utf7.decode(raw))
            return None
        last = 0 if full or not prev or prev[0] != uidvalidity else prev[1]
        typ, d = m.uid("SEARCH", None, "UID", f"{last + 1}:*")
        uids = [u for u in (d[0].split() if typ == "OK" and d and d[0] else []) if int(u) > last]
        max_uid = max([int(u) for u in uids] + [last, (uidnext or 1) - 1])
        return uids, uidvalidity, max_uid

    def _fetch_headers(self, m, uids):
        out = {}
        for i in range(0, len(uids), CHUNK):
            chunk = b",".join(uids[i:i + CHUNK]).decode()
            typ, data = m.uid("FETCH", chunk, HEADERS)
            for item in data or []:
                if not isinstance(item, tuple):
                    continue
                uid = re.search(rb"UID (\d+)", item[0])
                if not uid:
                    continue
                msg = email.message_from_bytes(item[1])
                sender = email.utils.parseaddr(_decode_header(msg.get("From", "")))[1].lower()
                flag = (msg.get("X-Spam-Flag") or "").strip().upper().startswith("YES") or \
                       (msg.get("X-Spam-Status") or "").strip().upper().startswith("YES")
                out[uid.group(1)] = (sender, _decode_header(msg.get("Subject", "")), flag)
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

    def pick_rule(self, sender, subject, flag):
        learned = self.learned.items if self.learned else frozenset()
        for rule in self.rules:
            if rule.learn and self.learn and not self.learn.allowed(sender):
                continue  # белый список / свои домены — не спам
            if rule.matches(sender, subject, flag, learned):
                return rule
        return None

    # ---------- learning ----------
    def _learn(self, m, folders):
        if not (self.learn and self.learned is not None):
            return []
        raw = folders.get(self.learn.folder)
        if not raw:
            return []
        res = self._new_uids(m, raw, full=False)
        if res is None:
            return []
        uids, uidvalidity, max_uid = res
        new = []
        if uids:
            senders = {s for s, _, _ in self._fetch_headers(m, uids).values() if self.learn.allowed(s)}
            new = self.learned.add(senders)
            if new:
                log.info("[%s] в спам-список добавлены: %s", self.login, ", ".join(new))
        m.close()
        self.state[raw] = [uidvalidity, max_uid]
        return new

    def _skip_own_moves(self, m, folders):
        """Письма, которые мы сами перенесли в папку спама, не должны попасть в обучение."""
        raw = folders.get(self.learn.folder)
        typ, st = m.status(_quote(raw), "(UIDNEXT UIDVALIDITY)")
        uidnext, uidvalidity = parse_status(st[0] if typ == "OK" and st else b"")
        if uidnext is not None:
            self.state[raw] = [uidvalidity, uidnext - 1]

    # ---------- main ----------
    def run_once(self, full=False) -> tuple[int, list]:
        """Один проход. full=True — все письма во всех папках, иначе только новые.
        Возвращает (перенесено писем, новые адреса в спам-списке)."""
        if not self.rules and not self.learn:
            return 0, []
        moved_total = 0
        moved_to_learn = False
        m = self._connect()
        try:
            folders = self._folders(m)
            learned_new = self._learn(m, folders)
            dest = {r.move_to: self._ensure_folder(m, folders, r.move_to) for r in self.rules}
            for name, raw in list(folders.items()):
                if name in self.skip:
                    continue
                res = self._new_uids(m, raw, full)
                if res is None:
                    continue
                uids, uidvalidity, max_uid = res
                if uids:
                    plan = {}
                    for uid, (sender, subject, flag) in self._fetch_headers(m, uids).items():
                        rule = self.pick_rule(sender, subject, flag)
                        if rule:
                            plan.setdefault((rule.move_to, rule.mark_read, rule.name), []).append(uid)
                    for (move_to, mark_read, rule_name), muids in plan.items():
                        self._move(m, muids, dest[move_to], mark_read)
                        moved_total += len(muids)
                        if self.learn and move_to == self.learn.folder:
                            moved_to_learn = True
                        log.info("[%s] %s: %s -> %s, писем %d", self.login, rule_name, name, move_to, len(muids))
                m.close()
                self.state[raw] = [uidvalidity, max_uid]
            if moved_to_learn:
                self._skip_own_moves(m, folders)
        finally:
            try:
                m.logout()
            except Exception:
                pass
        return moved_total, learned_new
