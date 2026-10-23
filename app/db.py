"""SQLite-хранилище: правила сортировки, их списки (адреса/домены/темы), отписки, контакты.

Статусы записей списка:
  active        — в фильтре
  unsubscribing — в фильтре, отписка отправлена, ждём неделю тишины
  unsubscribed  — отписались, из фильтра снята
  removed       — снята из фильтра вручную (бот / белый список)
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import yaml

from .config import Rule, _as_list, _norm_addr, _norm_domain

IN_FILTER = ("active", "unsubscribing")
EXEMPT = ("removed", "unsubscribed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY,
    mailbox TEXT NOT NULL,              -- адрес ящика или '*' = все ящики
    name TEXT NOT NULL,
    move_to TEXT NOT NULL,
    mark_read INTEGER NOT NULL DEFAULT 0,
    spam_header INTEGER NOT NULL DEFAULT 0,
    learn INTEGER NOT NULL DEFAULT 0,   -- сюда пишет обучение (папка = move_to)
    unsubscribe INTEGER NOT NULL DEFAULT 0,  -- отписка от рассылок (только спам)
    position INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(mailbox, name)
);
CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL REFERENCES rules(id),
    kind TEXT NOT NULL CHECK(kind IN ('addr', 'domain', 'subject')),
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',  -- seed / learned / manual / bot
    status TEXT NOT NULL DEFAULT 'active',
    hidden INTEGER NOT NULL DEFAULT 0,
    no_unsub INTEGER NOT NULL DEFAULT 0,
    created_at TEXT, updated_at TEXT, last_seen_at TEXT, unsub_at TEXT,
    UNIQUE(rule_id, kind, value)
);
CREATE TABLE IF NOT EXISTS unsubs(
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    mailbox TEXT, header TEXT, post TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending / done / failed
    result TEXT, created_at TEXT, done_at TEXT
);
CREATE TABLE IF NOT EXISTS domains(
    id INTEGER PRIMARY KEY,
    domain TEXT UNIQUE NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts(
    address TEXT PRIMARY KEY,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contact_boxes(
    address TEXT NOT NULL,
    mailbox TEXT NOT NULL,        -- из какого ящика мы ему писали
    sent INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(address, mailbox)
);
CREATE TABLE IF NOT EXISTS lookups(
    id INTEGER PRIMARY KEY,
    address TEXT NOT NULL,        -- «где его письма» — запрос из проверки адреса в боте
    boxes TEXT NOT NULL DEFAULT '{}',  -- {ящик: {"folders": {папка: писем}, "sent": наших ему, даты, letters}}
    created_at TEXT, sent_at TEXT,
    decision TEXT, decided_at TEXT,    -- кнопки под отчётом: spam / ham
    ai_at TEXT                         -- когда спрашивали нейронку
);
CREATE TABLE IF NOT EXISTS reports(
    id INTEGER PRIMARY KEY,
    sender TEXT NOT NULL,
    rule_id INTEGER,              -- в какое правило выучен
    mailbox TEXT,                 -- где кинули в папку
    action TEXT,                  -- added / returned
    boxes TEXT NOT NULL DEFAULT '{}',  -- {ящик: сколько старых писем перенесено}
    created_at TEXT, done_at TEXT
);
CREATE TABLE IF NOT EXISTS restores(
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL,
    needle TEXT NOT NULL,         -- адрес или @домен
    folder TEXT NOT NULL,         -- папка правила, откуда возвращаем
    rule_name TEXT, rule_mailbox TEXT,
    boxes TEXT NOT NULL DEFAULT '{}',  -- {ящик: сколько писем вернули во входящие}
    created_at TEXT, done_at TEXT
);
CREATE TABLE IF NOT EXISTS notices(
    id INTEGER PRIMARY KEY,
    sender TEXT NOT NULL,
    name TEXT, mailbox TEXT,      -- где сервер пометил спамом
    subject TEXT, summary TEXT,
    lu TEXT, lup TEXT,            -- List-Unsubscribe — на случай «оставить в спаме»
    letters INTEGER NOT NULL DEFAULT 1,
    texts TEXT NOT NULL DEFAULT '[]',  -- последние письма отправителя для сводки: [{date, subject, text}]
    boxes TEXT NOT NULL DEFAULT '{}',  -- {ящик: {"all": писем от него, "spam": из них в спаме, "sent": мы писали}}
    created_at TEXT, sent_at TEXT, decision TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def domain_of(addr: str) -> str:
    return addr.rsplit("@", 1)[1] if "@" in addr else ""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.q("PRAGMA table_info(rules)")}
        if "unsubscribe" not in cols:   # до появления обучаемых папок обучаемым было только правило спама
            self.x("ALTER TABLE rules ADD COLUMN unsubscribe INTEGER NOT NULL DEFAULT 0")
            self.x("UPDATE rules SET unsubscribe=1 WHERE learn=1")
        if "rule_id" not in {r["name"] for r in self.q("PRAGMA table_info(reports)")}:
            self.x("ALTER TABLE reports ADD COLUMN rule_id INTEGER")
        if "texts" not in {r["name"] for r in self.q("PRAGMA table_info(notices)")}:
            self.x("ALTER TABLE notices ADD COLUMN texts TEXT NOT NULL DEFAULT '[]'")
        cols = {r["name"] for r in self.q("PRAGMA table_info(lookups)")}
        for col in ("decision", "decided_at", "ai_at"):
            if col not in cols:
                self.x(f"ALTER TABLE lookups ADD COLUMN {col} TEXT")

    def q(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def x(self, sql, *args):
        return self.db.execute(sql, args)

    # ---------- meta ----------
    def meta(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", key)
        return r["value"] if r else default

    def set_meta(self, key, value):
        self.x("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               key, str(value))

    # ---------- rules & entries ----------
    def add_rule(self, mailbox, name, move_to, mark_read=False, spam_header=False, learn=False, position=100,
                 unsubscribe=False) -> int:
        self.x("INSERT OR IGNORE INTO rules(mailbox, name, move_to, mark_read, spam_header, learn, position, "
               "unsubscribe) VALUES(?,?,?,?,?,?,?,?)", mailbox, name, move_to, int(mark_read), int(spam_header),
               int(learn), position, int(unsubscribe))
        return self.one("SELECT id FROM rules WHERE mailbox=? AND name=?", mailbox, name)["id"]

    def add_entry(self, rule_id, kind, value, source="manual", status="active") -> int:
        value = _norm_domain(value) if kind == "domain" else (_norm_addr(value) if kind == "addr" else value.lower())
        ts = now_iso()
        self.x("INSERT OR IGNORE INTO entries(rule_id, kind, value, source, status, created_at, updated_at) "
               "VALUES(?,?,?,?,?,?,?)", rule_id, kind, value, source, status, ts, ts)
        return self.one("SELECT id FROM entries WHERE rule_id=? AND kind=? AND value=?", rule_id, kind, value)["id"]

    def rules_for(self, mailbox) -> list[Rule]:
        rows = self.q("SELECT * FROM rules WHERE enabled=1 AND mailbox IN ('*', ?) "
                      "ORDER BY position, mailbox != '*', id", mailbox)
        out = []
        for r in rows:
            rule = Rule(name=r["name"], move_to=r["move_to"], spam_header=bool(r["spam_header"]),
                        learn=bool(r["learn"]), mark_read=bool(r["mark_read"]), id=r["id"],
                        unsubscribe=bool(r["unsubscribe"]))
            for e in self.q("SELECT kind, value, status FROM entries WHERE rule_id=?", r["id"]):
                active = e["status"] in IN_FILTER
                if e["kind"] == "addr":
                    (rule.senders if active else rule.exempt_senders).add(e["value"])
                elif e["kind"] == "domain":
                    (rule.sender_domains if active else rule.exempt_domains).add(e["value"])
                elif active:
                    rule.subject_contains.append(e["value"])
            out.append(rule)
        return out

    def learn_rule(self):
        """Правило спама: обучаемое + с отпиской."""
        return self.one("SELECT * FROM rules WHERE enabled=1 AND learn=1 AND unsubscribe=1 ORDER BY id LIMIT 1")

    def learn_rules(self, mailbox):
        """Все обучаемые правила ящика (спам, БАНК, ДИСЛОКАЦИЯ, ...): папка = move_to."""
        return self.q("SELECT * FROM rules WHERE enabled=1 AND learn=1 AND mailbox IN ('*', ?) "
                      "ORDER BY position, mailbox != '*', id", mailbox)

    def touch_seen(self, rule_id, sender, now=None):
        """Письмо от sender попало под правило — обновляем «последний раз видели» (для недели тишины)."""
        ts = now_iso(now)
        self.x("UPDATE entries SET last_seen_at=? WHERE rule_id=? AND status IN ('active','unsubscribing') AND "
               "((kind='addr' AND value=?) OR (kind='domain' AND value=?))", ts, rule_id, sender, domain_of(sender))

    # ---------- learning & unsubscribe ----------
    def learn(self, sender, mailbox, list_unsub="", list_unsub_post="", now=None, rule_id=None):
        """Пользователь сам положил письмо от sender в обучаемую папку (по умолчанию — спам).
        Возвращает 'added' / 'returned' / None."""
        rule = self.one("SELECT * FROM rules WHERE id=?", rule_id) if rule_id else self.learn_rule()
        if not rule or not sender:
            return None
        ts = now_iso(now)
        # последнее слово за пользователем: из других обучаемых папок адрес снимаем
        self.x("UPDATE entries SET status='removed', updated_at=? WHERE kind='addr' AND value=? AND rule_id!=? AND "
               "status IN ('active','unsubscribing') AND rule_id IN (SELECT id FROM rules WHERE learn=1)",
               ts, sender, rule["id"])
        e = self.one("SELECT * FROM entries WHERE rule_id=? AND kind='addr' AND value=?", rule["id"], sender)
        action = None
        if e is None:
            dom = self.one("SELECT 1 FROM entries WHERE rule_id=? AND kind='domain' AND value=? AND status IN "
                           "('active','unsubscribing')", rule["id"], domain_of(sender))
            if dom:
                return None  # домен целиком уже в правиле
            self.x("INSERT INTO entries(rule_id, kind, value, source, status, created_at, updated_at) "
                   "VALUES(?, 'addr', ?, 'learned', 'active', ?, ?)", rule["id"], sender, ts, ts)
            action = "added"
        elif e["status"] in EXEMPT:
            no_unsub = 1 if e["status"] == "unsubscribed" else e["no_unsub"]
            self.x("UPDATE entries SET status='active', no_unsub=?, hidden=0, updated_at=? WHERE id=?",
                   no_unsub, ts, e["id"])
            action = "returned"
        if not rule["unsubscribe"]:
            return action
        e = self.one("SELECT * FROM entries WHERE rule_id=? AND kind='addr' AND value=?", rule["id"], sender)
        # подписка у каждого ящика своя — отписываем каждый ящик один раз
        if list_unsub and not e["no_unsub"] and e["status"] != "unsubscribed" and \
                not self.one("SELECT 1 FROM unsubs WHERE entry_id=? AND mailbox=?", e["id"], mailbox):
            self.x("INSERT INTO unsubs(entry_id, mailbox, header, post, created_at) VALUES(?,?,?,?,?)",
                   e["id"], mailbox, list_unsub, list_unsub_post or "", ts)
        return action

    def pending_unsubs(self):
        return self.q("SELECT u.*, e.value AS sender FROM unsubs u JOIN entries e ON e.id=u.entry_id "
                      "WHERE u.status='pending' ORDER BY u.id")

    def finish_unsub(self, unsub_id, ok: bool, result: str, now=None):
        ts = now_iso(now)
        u = self.one("SELECT * FROM unsubs WHERE id=?", unsub_id)
        self.x("UPDATE unsubs SET status=?, result=?, done_at=? WHERE id=?", "done" if ok else "failed",
               result, ts, unsub_id)
        if ok:
            self.x("UPDATE entries SET status='unsubscribing', unsub_at=?, updated_at=? WHERE id=? "
                   "AND status='active'", ts, ts, u["entry_id"])
        else:
            self.x("UPDATE entries SET unsub_at=? WHERE id=?", ts, u["entry_id"])

    def expire_unsubscribing(self, days=7, now=None) -> list[str]:
        """Неделя тишины после отписки — снимаем с фильтра в список отписанных."""
        now = now or datetime.now(timezone.utc)
        border = now_iso(now - timedelta(days=days))
        rows = self.q("SELECT id, value FROM entries WHERE status='unsubscribing' AND "
                      "max(unsub_at, coalesce(last_seen_at, '')) < ?", border)
        for r in rows:
            self.x("UPDATE entries SET status='unsubscribed', updated_at=? WHERE id=?", now_iso(now), r["id"])
        return [r["value"] for r in rows]

    # ---------- отчёты о новых адресах в спам-фильтре ----------
    def open_report(self, sender, mailbox, action, rule_id=None, now=None):
        if rule_id is None:
            lr = self.learn_rule()
            rule_id = lr["id"] if lr else None
        if not self.one("SELECT 1 FROM reports WHERE sender=? AND rule_id IS ? AND done_at IS NULL", sender, rule_id):
            self.x("INSERT INTO reports(sender, rule_id, mailbox, action, created_at) VALUES(?,?,?,?,?)",
                   sender, rule_id, mailbox, action, now_iso(now))

    def has_open_report(self, sender) -> bool:
        return bool(self.one("SELECT 1 FROM reports WHERE sender=? AND done_at IS NULL", sender))

    def report_progress(self, mailbox, swept_keys, moved_by_sender):
        """Ящик прочесал старую почту по адресу — записываем, сколько писем перенёс."""
        spam = self.learn_rule()
        for r in self.q("SELECT id, sender, boxes, rule_id FROM reports WHERE done_at IS NULL"):
            rid = r["rule_id"] or (spam["id"] if spam else None)
            boxes = json.loads(r["boxes"])
            if mailbox not in boxes and f"{rid}:a:{r['sender']}" in swept_keys:
                boxes[mailbox] = moved_by_sender.get(r["sender"], 0)
                self.x("UPDATE reports SET boxes=? WHERE id=?", json.dumps(boxes, ensure_ascii=False), r["id"])

    def ready_reports(self, mailboxes, timeout_min=30, now=None):
        """Отчёты, по которым отметились все ящики (или вышел таймаут) и отписка уже отработала."""
        now = now or datetime.now(timezone.utc)
        spam = self.learn_rule()
        out = []
        for r in self.q("SELECT * FROM reports WHERE done_at IS NULL ORDER BY id"):
            boxes = json.loads(r["boxes"])
            timed_out = r["created_at"] < now_iso(now - timedelta(minutes=timeout_min))
            rule = self.one("SELECT * FROM rules WHERE id=?", r["rule_id"] or (spam["id"] if spam else 0))
            entry = self.one("SELECT id, no_unsub FROM entries WHERE rule_id=? AND kind='addr' AND value=?",
                             rule["id"] if rule else 0, r["sender"])
            unsubs = self.q("SELECT mailbox, status, result FROM unsubs WHERE entry_id=? ORDER BY id",
                            entry["id"]) if entry else []
            if any(u["status"] == "pending" for u in unsubs) and not timed_out:
                continue
            if set(mailboxes) <= set(boxes) or timed_out:
                out.append({"id": r["id"], "sender": r["sender"], "mailbox": r["mailbox"], "action": r["action"],
                            "boxes": boxes, "missing": sorted(set(mailboxes) - set(boxes)),
                            "unsubs": [dict(u) for u in unsubs], "no_unsub": bool(entry and entry["no_unsub"]),
                            "folder": rule["move_to"] if rule else "?",
                            "spam": bool(rule and rule["unsubscribe"])})
        return out

    def close_report(self, report_id, now=None):
        self.x("UPDATE reports SET done_at=? WHERE id=?", now_iso(now), report_id)

    # ---------- возврат писем во входящие (кнопка в боте) ----------
    def queue_restore(self, entry_id, now=None):
        """Снять запись с правила и поставить в очередь возврат её писем во входящие."""
        e = self.one("SELECT e.*, r.name AS rule_name, r.move_to, r.mailbox AS rmb FROM entries e "
                     "JOIN rules r ON r.id=e.rule_id WHERE e.id=?", entry_id)
        if not e:
            return None
        ts = now_iso(now)
        self.x("UPDATE entries SET status='removed', hidden=0, updated_at=? WHERE id=?", ts, entry_id)
        needle = e["value"] if e["kind"] == "addr" else "@" + e["value"]
        if not self.one("SELECT 1 FROM restores WHERE entry_id=? AND done_at IS NULL", entry_id):
            self.x("INSERT INTO restores(entry_id, needle, folder, rule_name, rule_mailbox, created_at) "
                   "VALUES(?,?,?,?,?,?)", entry_id, needle, e["move_to"], e["rule_name"], e["rmb"], ts)
        return e

    def pending_restores(self, mailbox):
        out = []
        for r in self.q("SELECT * FROM restores WHERE done_at IS NULL AND rule_mailbox IN ('*', ?)", mailbox):
            if mailbox not in json.loads(r["boxes"]):
                out.append(r)
        return out

    def restore_progress(self, restore_id, mailbox, n):
        r = self.one("SELECT boxes FROM restores WHERE id=?", restore_id)
        boxes = json.loads(r["boxes"])
        boxes[mailbox] = n
        self.x("UPDATE restores SET boxes=? WHERE id=?", json.dumps(boxes, ensure_ascii=False), restore_id)

    def ready_restores(self, mailboxes, timeout_min=30, now=None):
        now = now or datetime.now(timezone.utc)
        out = []
        for r in self.q("SELECT * FROM restores WHERE done_at IS NULL ORDER BY id"):
            need = set(mailboxes) if r["rule_mailbox"] == "*" else {r["rule_mailbox"]}
            boxes = json.loads(r["boxes"])
            timed_out = r["created_at"] < now_iso(now - timedelta(minutes=timeout_min))
            if need <= set(boxes) or timed_out:
                out.append(dict(r, boxes=boxes, missing=sorted(need - set(boxes))))
        return out

    def close_restore(self, restore_id, now=None):
        self.x("UPDATE restores SET done_at=? WHERE id=?", now_iso(now), restore_id)

    # ---------- серверный спам: уведомление с выбором ----------
    def open_notice(self, sender, name, mailbox, subject, summary, lu="", lup="", now=None):
        """Сервер пометил письмо спамом, мы унесли его в спам — спросить пользователя. Одно уведомление на отправителя."""
        n = self.one("SELECT id, sent_at FROM notices WHERE sender=? AND decision IS NULL", sender)
        if n:
            self.x("UPDATE notices SET letters=letters+1 WHERE id=?", n["id"])
            return n["id"]
        self.x("INSERT INTO notices(sender, name, mailbox, subject, summary, lu, lup, created_at) "
               "VALUES(?,?,?,?,?,?,?,?)", sender, name, mailbox, subject, summary, lu, lup, now_iso(now))
        return self.one("SELECT last_insert_rowid() AS id")["id"]

    def pending_notices(self, mailbox):
        """Уведомления, по которым этот ящик ещё не посчитал письма отправителя."""
        return [n for n in self.q("SELECT * FROM notices WHERE sent_at IS NULL AND decision IS NULL")
                if mailbox not in json.loads(n["boxes"])]

    def notice_add_texts(self, notice_id, letters, keep=10):
        """Копим тексты последних писем отправителя (из всех ящиков), без дублей, свежие первыми."""
        n = self.one("SELECT texts FROM notices WHERE id=?", notice_id)
        cur = json.loads(n["texts"])
        seen = {(x["subject"], x["text"][:200]) for x in cur}
        for x in letters:
            k = (x["subject"], x["text"][:200])
            if x["text"].strip() and k not in seen:
                seen.add(k)
                cur.append({"date": x.get("date", ""), "subject": x["subject"], "text": x["text"][:4000]})
        cur.sort(key=lambda x: x["date"], reverse=True)
        self.x("UPDATE notices SET texts=? WHERE id=?", json.dumps(cur[:keep], ensure_ascii=False), notice_id)

    def notice_progress(self, notice_id, mailbox, info: dict):
        n = self.one("SELECT boxes FROM notices WHERE id=?", notice_id)
        boxes = json.loads(n["boxes"])
        boxes[mailbox] = info
        self.x("UPDATE notices SET boxes=? WHERE id=?", json.dumps(boxes, ensure_ascii=False), notice_id)

    def ready_notices(self, mailboxes, timeout_min=30, now=None):
        now = now or datetime.now(timezone.utc)
        out = []
        for n in self.q("SELECT * FROM notices WHERE sent_at IS NULL AND decision IS NULL ORDER BY id"):
            boxes = json.loads(n["boxes"])
            timed_out = n["created_at"] < now_iso(now - timedelta(minutes=timeout_min))
            if set(mailboxes) <= set(boxes) or timed_out:
                out.append(dict(n, boxes=boxes, texts=json.loads(n["texts"]),
                                missing=sorted(set(mailboxes) - set(boxes))))
        return out

    def mark_notice_sent(self, notice_id, now=None):
        self.x("UPDATE notices SET sent_at=? WHERE id=?", now_iso(now), notice_id)

    def decide_notice(self, notice_id, decision, now=None):
        """keep — учимся как на ручном переносе в спам; release — исключение от метки + возврат во входящие."""
        n = self.one("SELECT * FROM notices WHERE id=?", notice_id)
        if not n or n["decision"]:
            return n, None
        rule = self.learn_rule()
        self.x("UPDATE notices SET decision=?, decided_at=? WHERE id=?", decision, now_iso(now), notice_id)
        if decision == "keep":
            action = self.learn(n["sender"], n["mailbox"], n["lu"] or "", n["lup"] or "", now=now)
            self.open_report(n["sender"], n["mailbox"], action or "added", rule_id=rule["id"], now=now)
        else:
            eid = self.add_entry(rule["id"], "addr", n["sender"], "bot")
            self.queue_restore(eid, now=now)
        return self.one("SELECT * FROM notices WHERE id=?", notice_id), decision

    # ---------- «где его письма»: поиск по всем ящикам и папкам ----------
    def queue_lookup(self, address, now=None):
        """Одна незавершённая проверка на адрес — повторные нажатия не плодят отчёты."""
        address = _norm_addr(address)
        r = self.one("SELECT id FROM lookups WHERE address=? AND sent_at IS NULL", address)
        if r:
            return r["id"]
        self.x("INSERT INTO lookups(address, created_at) VALUES(?, ?)", address, now_iso(now))
        return self.one("SELECT last_insert_rowid() AS id")["id"]

    def pending_lookups(self, mailbox):
        return [r for r in self.q("SELECT * FROM lookups WHERE sent_at IS NULL ORDER BY id")
                if mailbox not in json.loads(r["boxes"])]

    def lookup_progress(self, lookup_id, mailbox, info: dict):
        r = self.one("SELECT boxes FROM lookups WHERE id=?", lookup_id)
        boxes = json.loads(r["boxes"])
        boxes[mailbox] = info
        self.x("UPDATE lookups SET boxes=? WHERE id=?", json.dumps(boxes, ensure_ascii=False), lookup_id)

    def ready_lookups(self, mailboxes, timeout_min=30, now=None):
        now = now or datetime.now(timezone.utc)
        out = []
        for r in self.q("SELECT * FROM lookups WHERE sent_at IS NULL ORDER BY id"):
            boxes = json.loads(r["boxes"])
            timed_out = r["created_at"] < now_iso(now - timedelta(minutes=timeout_min))
            if set(mailboxes) <= set(boxes) or timed_out:
                out.append(dict(r, boxes=boxes, missing=sorted(set(mailboxes) - set(boxes))))
        return out

    def close_lookup(self, lookup_id, now=None):
        self.x("UPDATE lookups SET sent_at=? WHERE id=?", now_iso(now), lookup_id)

    def get_lookup(self, lookup_id):
        r = self.one("SELECT * FROM lookups WHERE id=?", lookup_id)
        return dict(r, boxes=json.loads(r["boxes"])) if r else None

    def mark_lookup_ai(self, lookup_id, now=None):
        self.x("UPDATE lookups SET ai_at=? WHERE id=?", now_iso(now), lookup_id)

    def purge_lookup_letters(self, days=3, now=None):
        """Тексты писем держим только пару дней — для кнопки «сводка нейронкой», дальше не копим переписку."""
        edge = now_iso((now or datetime.now(timezone.utc)) - timedelta(days=days))
        for r in self.q("SELECT id, boxes FROM lookups WHERE created_at < ? AND boxes LIKE '%\"letters\"%'", edge):
            boxes = json.loads(r["boxes"])
            for info in boxes.values():
                info.pop("letters", None)
            self.x("UPDATE lookups SET boxes=? WHERE id=?", json.dumps(boxes, ensure_ascii=False), r["id"])

    def decide_lookup(self, lookup_id, decision, now=None):
        """Кнопки под отчётом «где его письма». spam — как ручной перенос в спам во всех ящиках, где есть его
        письма (с отпиской); ham — исключение от спам-фильтра и серверной метки + возврат во входящие."""
        r = self.get_lookup(lookup_id)
        if not r or r["decision"]:
            return r, None
        rule = self.learn_rule()
        if not rule:
            return r, None
        addr = r["address"]
        self.x("UPDATE lookups SET decision=?, decided_at=? WHERE id=?", decision, now_iso(now), lookup_id)
        if decision == "spam":
            boxes = sorted(r["boxes"].items(), key=lambda x: -sum(x[1].get("folders", {}).values()))
            action = None
            for mb, info in boxes:
                if info.get("folders"):
                    action = self.learn(addr, mb, info.get("lu", ""), info.get("lup", ""), now=now) or action
            if not any(info.get("folders") for _, info in boxes):
                action = self.learn(addr, "", now=now)
            self.open_report(addr, boxes[0][0] if boxes else "", action or "added", rule_id=rule["id"], now=now)
        else:
            self.queue_restore(self.add_entry(rule["id"], "addr", addr, "bot"), now=now)
        return self.get_lookup(lookup_id), decision

    # ---------- contacts (для оценки полезности) ----------
    def add_contacts(self, addresses):
        for a in addresses:
            a = _norm_addr(a)
            if "@" in a:
                self.x("INSERT INTO contacts(address, sent) VALUES(?, 1) "
                       "ON CONFLICT(address) DO UPDATE SET sent=sent+1", a)

    def add_box_contacts(self, addresses, mailbox):
        for a in addresses:
            a = _norm_addr(a)
            if "@" in a:
                self.x("INSERT INTO contact_boxes(address, mailbox, sent) VALUES(?, ?, 1) "
                       "ON CONFLICT(address, mailbox) DO UPDATE SET sent=sent+1", a, mailbox)

    def box_contacts(self, address) -> dict:
        """{ящик: сколько раз мы ему писали из этого ящика}"""
        return {r["mailbox"]: r["sent"] for r in
                self.q("SELECT mailbox, sent FROM contact_boxes WHERE address=?", _norm_addr(address))}

    def contacts(self):
        by_addr, by_domain = {}, {}
        for r in self.q("SELECT address, sent FROM contacts"):
            by_addr[r["address"]] = r["sent"]
            d = domain_of(r["address"])
            by_domain[d] = by_domain.get(d, 0) + r["sent"]
        return by_addr, by_domain

    # ---------- первичный перенос из rules.yaml + learned_spam.txt ----------
    def seed(self, rules_file, learned_file=None) -> bool:
        if self.meta("seeded"):
            return False
        from .config import _parse_rules
        data = {}
        if rules_file and os.path.exists(rules_file):
            with open(rules_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        learn_id = None
        for mailbox, conf in data.items():
            if isinstance(conf, list):
                conf = {"rules": conf}
            mb = "*" if mailbox == "_all" else _norm_addr(mailbox)
            for pos, rule in enumerate(_parse_rules(conf.get("rules"), mailbox)):
                rid = self.add_rule(mb, rule.name, rule.move_to, rule.mark_read, rule.spam_header, rule.learn,
                                    position=(pos + 1) * 10, unsubscribe=rule.learn and rule.spam_header)
                if rule.learn and learn_id is None:
                    learn_id = rid
                for a in rule.senders:
                    self.add_entry(rid, "addr", a, "seed")
                for d in rule.sender_domains:
                    self.add_entry(rid, "domain", d, "seed")
                for s in rule.subject_contains:
                    self.add_entry(rid, "subject", s, "seed")
            if mb == "*" and learn_id and isinstance(conf.get("learn_spam"), dict):
                for x in _as_list(conf["learn_spam"].get("never")):
                    kind = "addr" if "@" in x.lstrip("@") else "domain"
                    self.add_entry(learn_id, kind, x, "seed", status="removed")
        if learn_id and learned_file and os.path.exists(learned_file):
            with open(learned_file, encoding="utf-8") as f:
                for line in f:
                    a = line.split("#", 1)[0].strip().lower()
                    if "@" in a:
                        self.add_entry(learn_id, "addr", a, "learned")
        self.set_meta("seeded", now_iso())
        return True
