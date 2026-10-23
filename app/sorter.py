"""Раскладка писем по папкам на IMAP-сервере согласно правилам."""
import email
import email.header
import email.utils
import imaplib
import logging
import re
from collections import Counter, namedtuple

from . import imap_utf7
from .config import DEFAULT_SKIP_FOLDERS, LearnSpam, Rule
from .snippet import body_text

log = logging.getLogger("sorter")
LIST_RE = re.compile(rb'\((?P<flags>.*?)\) "(?P<sep>.)" (?P<name>.*)')
STATUS_RE = re.compile(rb"UIDNEXT (\d+).*?UIDVALIDITY (\d+)|UIDVALIDITY (\d+).*?UIDNEXT (\d+)")
HEADERS = ("(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT X-SPAM-FLAG X-SPAM-STATUS "
           "LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])")
RCPT_HEADERS = "(BODY.PEEK[HEADER.FIELDS (TO CC BCC)])"
SENT_FOLDER = "Отправленные"
CHUNK = 500

Hdr = namedtuple("Hdr", "sender subject flag lu lup name")


def _clean(v) -> str:
    return " ".join(str(v or "").split())


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
    def __init__(self, host, port, login, password, store, box_state: dict, own_domains=(), timeout=120):
        self.host, self.port = host, port
        self.login, self.password = login, password
        self.store = store
        self.state = box_state            # {raw_folder: [uidvalidity, last_uid]}
        self.own_domains = set(own_domains)
        self.timeout = timeout
        self.caps = set()
        self.rules: list[Rule] = []
        self.learn: LearnSpam | None = None
        self.skip = set(DEFAULT_SKIP_FOLDERS)
        self.moved_by_sender = Counter()  # сколько писем каждого отправителя перенесено за проход
        self.learn_folders: dict[str, int] = {}
        self.contacts: set = set()        # кому мы писали — их серверная метка спама не трогает

    def reload_rules(self):
        """Правила читаются из базы перед каждым проходом — правки из бота применяются через минуту."""
        self.rules = self.store.rules_for(self.login)
        lr = self.store.learn_rule()
        self.learn = LearnSpam(folder=lr["move_to"], never_domains=self.own_domains) if lr else None
        # обучаемые папки: спам, БАНК, ДИСЛОКАЦИЯ, ... -> id правила
        self.learn_folders = {r["move_to"]: r["id"] for r in self.store.learn_rules(self.login)}
        self.skip = set(DEFAULT_SKIP_FOLDERS) | {r.move_to for r in self.rules} | set(self.learn_folders)
        self.contacts = set(self.store.contacts()[0])

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

    def _new_uids(self, m, raw, full, key=None):
        """Выбирает папку и возвращает (uids к проверке, uidvalidity, max_uid). None — папку пропустить."""
        prev = self.state.get(key or raw)
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
                name, sender = email.utils.parseaddr(_decode_header(msg.get("From", "")))
                sender = sender.lower()
                flag = (msg.get("X-Spam-Flag") or "").strip().upper().startswith("YES") or \
                       (msg.get("X-Spam-Status") or "").strip().upper().startswith("YES")
                out[uid.group(1)] = Hdr(sender, _decode_header(msg.get("Subject", "")), flag,
                                        _clean(msg.get("List-Unsubscribe")), _clean(msg.get("List-Unsubscribe-Post")),
                                        name.strip())
        return out

    def _fetch_recipients(self, m, uids):
        found = []
        for i in range(0, len(uids), CHUNK):
            typ, data = m.uid("FETCH", b",".join(uids[i:i + CHUNK]).decode(), RCPT_HEADERS)
            for item in data or []:
                if isinstance(item, tuple):
                    msg = email.message_from_bytes(item[1])
                    vals = [_decode_header(v) for h in ("To", "Cc", "Bcc") for v in msg.get_all(h, [])]
                    found += [a.lower() for _, a in email.utils.getaddresses(vals) if "@" in a]
        return found

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

    @staticmethod
    def by_server_flag(rule, h: Hdr) -> bool:
        """Письмо попало под правило только из-за серверной метки спама (не по нашим спискам)."""
        return rule.spam_header and h.flag and not rule.matches(h.sender, h.subject, False)

    def pick_rule(self, h: Hdr):
        for rule in self.rules:
            if rule.unsubscribe and self.learn and not self.learn.allowed(h.sender):
                continue  # свои домены никогда не спам
            if not rule.matches(h.sender, h.subject, h.flag):
                continue
            if self.by_server_flag(rule, h) and h.sender in self.contacts:
                continue  # мы ему писали — одной метки сервера мало
            return rule
        return None

    def _fetch_letters(self, m, uids, limit=5):
        """Последние письма (по UID в папке): [{date, subject, text}] — для сводки в уведомлении."""
        out = []
        for uid in sorted(uids, key=int, reverse=True)[:limit]:
            uid = uid.decode() if isinstance(uid, bytes) else str(uid)
            typ, data = m.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[]<0.60000>)")
            for item in data or []:
                if isinstance(item, tuple):
                    date = imaplib.Internaldate2tuple(item[0])
                    try:
                        msg = email.message_from_bytes(item[1])
                        text = body_text(item[1])
                    except Exception:
                        continue
                    out.append({"date": "%04d-%02d-%02dT%02d:%02d:%02d" % date[:6] if date else "",
                                "subject": _decode_header(msg.get("Subject", "")), "text": text})
        return out

    # ---------- learning ----------
    def _learn(self, m, folders):
        """Новые письма в обучаемых папках (спам, БАНК, ...) положил туда пользователь — учимся.
        Свои домены не учатся никогда (их можно добавить в правило вручную)."""
        events = []
        own = LearnSpam(never_domains=self.own_domains)
        for folder, rule_id in self.learn_folders.items():
            raw = folders.get(folder)
            if not raw:
                continue
            res = self._new_uids(m, raw, full=False)
            if res is None:
                continue
            uids, uidvalidity, max_uid = res
            if uids:
                for h in self._fetch_headers(m, uids).values():
                    if own.allowed(h.sender):
                        action = self.store.learn(h.sender, self.login, h.lu, h.lup, rule_id=rule_id)
                        if action:
                            events.append((h.sender, action))
                            self.store.open_report(h.sender, self.login, action, rule_id=rule_id)
                            log.info("[%s] %s: %s %s", self.login, folder,
                                     "добавлен" if action == "added" else "возвращён", h.sender)
            m.close()
            self.state[raw] = [uidvalidity, max_uid]
        return events

    def _collect_sent(self, m, folders):
        """Кому мы писали — для оценки «полезности» адресов в боте."""
        raw = folders.get(SENT_FOLDER)
        if not raw:
            return 0
        key = "sent:" + raw
        res = self._new_uids(m, raw, full=False, key=key)
        if res is None:
            return 0
        uids, uidvalidity, max_uid = res
        found = self._fetch_recipients(m, uids) if uids else []
        self.store.add_contacts(found)
        m.close()
        self.state[key] = [uidvalidity, max_uid]
        return len(found)

    def _skip_own_moves(self, m, folders, moved_into):
        """Письма, которые мы сами перенесли в обучаемые папки, не должны попасть в обучение."""
        for folder in moved_into:
            raw = folders.get(folder)
            if not raw:
                continue
            typ, st = m.status(_quote(raw), "(UIDNEXT UIDVALIDITY)")
            uidnext, uidvalidity = parse_status(st[0] if typ == "OK" and st else b"")
            if uidnext is not None:
                self.state[raw] = [uidvalidity, uidnext - 1]

    # ---------- догоняющий поиск по новым адресам ----------
    SWEPT_KEY = "_swept"

    def _targets(self) -> dict:
        """Адреса и домены всех правил ящика: ключ -> строка для IMAP SEARCH FROM."""
        out = {}
        for r in self.rules:
            for a in r.senders:
                out[f"{r.id}:a:{a}"] = a
            for d in r.sender_domains:
                out[f"{r.id}:d:{d}"] = "@" + d
        return out

    def _sweep(self, m, folders, dest, needles) -> tuple[int, set]:
        """Старые письма от только что добавленных адресов/доменов — ищем на сервере и переносим,
        не дожидаясь ночного полного прохода. Решение всё равно принимает pick_rule по заголовкам."""
        moved, to_learn = 0, set()
        for name, raw in folders.items():
            if name in self.skip:
                continue
            typ, _ = m.select(_quote(raw))
            if typ != "OK":
                continue
            found = set()
            for needle in needles:
                typ, d = m.uid("SEARCH", None, "FROM", _quote(needle))
                if typ == "OK" and d and d[0]:
                    found.update(d[0].split())
            if found:
                plan = {}
                for uid, h in self._fetch_headers(m, sorted(found, key=int)).items():
                    rule = self.pick_rule(h)
                    if rule:
                        plan.setdefault(rule.id or rule.name, (rule, []))[1].append((uid, h.sender))
                for rule, items in plan.values():
                    uids = [u for u, _ in items]
                    self._move(m, uids, dest[rule.move_to], rule.mark_read)
                    self.moved_by_sender.update(sender for _, sender in items)
                    moved += len(uids)
                    if rule.move_to in self.learn_folders:
                        to_learn.add(rule.move_to)
                    log.info("[%s] %s (новые адреса, старые письма): %s -> %s, писем %d",
                             self.login, rule.name, name, rule.move_to, len(uids))
            m.close()
        return moved, to_learn

    # ---------- серверный спам ----------
    def _server_spam_notices(self, m, flagged):
        """Сервер пометил спамом, мы уносим — уведомление пользователю (тема, суть, переписка) с выбором."""
        for uid, h in flagged:
            nid = self.store.open_notice(h.sender, h.name, self.login, h.subject, "", h.lu, h.lup)
            self.store.notice_add_texts(nid, self._fetch_letters(m, [uid], limit=1))
            log.info("[%s] серверная метка спама: %s — «%s»", self.login, h.sender, h.subject[:80])

    def _notices(self, m, folders):
        """По каждому новому уведомлению: сколько писем от отправителя в этом ящике и писали ли мы ему."""
        for n in self.store.pending_notices(self.login):
            sender = n["sender"]
            info = {"all": 0, "spam": 0, "sent": 0}
            for name, raw in folders.items():
                if name in DEFAULT_SKIP_FOLDERS and (not self.learn or name != self.learn.folder):
                    continue
                typ, _ = m.select(_quote(raw), readonly=True)
                if typ != "OK":
                    continue
                if name == SENT_FOLDER:
                    typ, d = m.uid("SEARCH", None, "TO", _quote(sender))
                    info["sent"] += len(d[0].split()) if typ == "OK" and d and d[0] else 0
                else:
                    typ, d = m.uid("SEARCH", None, "FROM", _quote(sender))
                    found = d[0].split() if typ == "OK" and d and d[0] else []
                    mine = [uid for uid, h in self._fetch_headers(m, found).items() if h.sender == sender]
                    info["all"] += len(mine)
                    if self.learn and name == self.learn.folder:
                        info["spam"] += len(mine)
                    if mine:
                        self.store.notice_add_texts(n["id"], self._fetch_letters(m, mine))
                m.close()
            self.store.notice_progress(n["id"], self.login, info)

    # ---------- возврат во входящие ----------
    def _restores(self, m, folders):
        """Кнопка «убрать из фильтра» в боте: письма отправителя из папки правила — обратно во входящие."""
        for job in self.store.pending_restores(self.login):
            raw = folders.get(job["folder"])
            n = 0
            if raw:
                typ, _ = m.select(_quote(raw))
                if typ == "OK":
                    typ, d = m.uid("SEARCH", None, "FROM", _quote(job["needle"]))
                    found = d[0].split() if typ == "OK" and d and d[0] else []
                    needle = job["needle"].lower()
                    uids = [uid for uid, h in self._fetch_headers(m, found).items()
                            if (h.sender == needle if not needle.startswith("@") else h.sender.endswith(needle))]
                    if uids:
                        self._move(m, uids, "INBOX", mark_read=False)
                        n = len(uids)
                        log.info("[%s] возврат %s: %s -> INBOX, писем %d", self.login, job["needle"], job["folder"], n)
                    m.close()
            self.store.restore_progress(job["id"], self.login, n)

    # ---------- main ----------
    def run_once(self, full=False) -> tuple[int, list]:
        """Один проход. full=True — все письма во всех папках, иначе только новые.
        Возвращает (перенесено писем, события обучения [(адрес, 'added'|'returned')])."""
        self.reload_rules()
        if not self.rules and not self.learn_folders:
            return 0, []
        moved_total = 0
        moved_to_learn = set()
        self.moved_by_sender = Counter()
        m = self._connect()
        try:
            folders = self._folders(m)
            events = self._learn(m, folders)
            if events:
                self.reload_rules()  # выученные адреса работают уже в этом проходе
            self._restores(m, folders)
            self._collect_sent(m, folders)
            dest = {r.move_to: self._ensure_folder(m, folders, r.move_to) for r in self.rules}
            for name, raw in list(folders.items()):
                if name in self.skip:
                    continue
                res = self._new_uids(m, raw, full)
                if res is None:
                    continue
                uids, uidvalidity, max_uid = res
                if uids:
                    plan, flagged = {}, []
                    for uid, h in self._fetch_headers(m, uids).items():
                        rule = self.pick_rule(h)
                        if rule:
                            plan.setdefault(rule.id or rule.name, (rule, []))[1].append((uid, h.sender))
                            if rule.unsubscribe and self.by_server_flag(rule, h):
                                flagged.append((uid, h))
                    if flagged:
                        self._server_spam_notices(m, flagged)
                    for rule, items in plan.values():
                        self._move(m, [u for u, _ in items], dest[rule.move_to], rule.mark_read)
                        if rule.id:
                            for sender in {s for _, s in items}:
                                self.store.touch_seen(rule.id, sender)
                        moved_total += len(items)
                        self.moved_by_sender.update(sender for _, sender in items)
                        if rule.move_to in self.learn_folders:
                            moved_to_learn.add(rule.move_to)
                        log.info("[%s] %s: %s -> %s, писем %d", self.login, rule.name, name, rule.move_to, len(items))
                m.close()
                self.state[raw] = [uidvalidity, max_uid]
            targets = self._targets()
            if full:
                self.state[self.SWEPT_KEY] = sorted(targets)   # полный проход уже всё перебрал
            else:
                # снятые с фильтра забываем — если вернут, старые письма прочешем заново
                swept = set(self.state.get(self.SWEPT_KEY, [])) & set(targets)
                self.state[self.SWEPT_KEY] = sorted(swept)
                fresh = [k for k in targets if k not in swept]
                if fresh:
                    n, to_learn = self._sweep(m, folders, dest, sorted({targets[k] for k in fresh}))
                    moved_total += n
                    moved_to_learn |= to_learn
                    self.state[self.SWEPT_KEY] = sorted(swept | set(fresh))
            if moved_to_learn:
                self._skip_own_moves(m, folders, moved_to_learn)
            if self.learn:
                self._notices(m, folders)   # после переносов — чтобы посчитать и только что пойманное
            if self.learn_folders:
                self.store.report_progress(self.login, set(self.state.get(self.SWEPT_KEY, [])), self.moved_by_sender)
        finally:
            try:
                m.logout()
            except Exception:
                pass
        return moved_total, events
