"""Загрузка настроек из переменных окружения и правил из YAML."""
import json
import os
from dataclasses import dataclass, field

import yaml

# Папки, из которых никогда ничего не забираем (корзина, спам, черновики).
DEFAULT_SKIP_FOLDERS = ["Удаленные элементы", "Нежелательная почта", "Черновики"]
GLOBAL_KEY = "_all"  # правила из этого блока применяются ко всем ящикам из MAILBOXES


def _norm_addr(a: str) -> str:
    return (a or "").strip().lower()


def _norm_domain(d: str) -> str:
    return (d or "").strip().lower().lstrip("@")


@dataclass
class Rule:
    name: str
    move_to: str
    senders: set = field(default_factory=set)        # точные адреса
    sender_domains: set = field(default_factory=set)  # домены (без @)
    subject_contains: list = field(default_factory=list)
    spam_header: bool = False   # срабатывать на серверную метку спама (X-Spam-Flag / X-Spam-Status: yes)
    learn: bool = False         # дополнять адреса из обученного списка спама
    mark_read: bool = False

    def matches(self, sender: str, subject: str, spam_flag: bool = False, learned: frozenset = frozenset()) -> bool:
        sender = _norm_addr(sender)
        if sender and (sender in self.senders or (self.learn and sender in learned)):
            return True
        if sender and "@" in sender and sender.rsplit("@", 1)[1] in self.sender_domains:
            return True
        if self.spam_header and spam_flag:
            return True
        subj = (subject or "").lower()
        return any(s in subj for s in self.subject_contains)

    @property
    def has_conditions(self) -> bool:
        return bool(self.senders or self.sender_domains or self.subject_contains or self.spam_header or self.learn)


@dataclass
class LearnSpam:
    folder: str = "Нежелательная почта"
    never_senders: set = field(default_factory=set)
    never_domains: set = field(default_factory=set)

    def allowed(self, sender: str) -> bool:
        """Можно ли считать адрес спамом (не в белом списке)."""
        sender = _norm_addr(sender)
        if not sender or "@" not in sender:
            return False
        return sender not in self.never_senders and sender.rsplit("@", 1)[1] not in self.never_domains


@dataclass
class Settings:
    mailboxes: dict
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    poll_interval: int
    rules_file: str
    data_dir: str
    notify_to: str
    notify_from: str
    notify_cooldown: int
    full_scan_hour: int


def load_settings(env=os.environ) -> Settings:
    raw = env.get("MAILBOXES", "").strip()
    if not raw:
        raise RuntimeError("MAILBOXES не задан")
    mailboxes = {k.strip().lower(): v for k, v in json.loads(raw).items()}
    notify_from = env.get("NOTIFY_FROM", "").strip().lower()
    return Settings(
        mailboxes=mailboxes,
        imap_host=env["IMAP_HOST"],
        imap_port=int(env.get("IMAP_PORT", "993")),
        smtp_host=env.get("SMTP_HOST") or env["IMAP_HOST"],
        smtp_port=int(env.get("SMTP_PORT", "465")),
        poll_interval=int(env.get("POLL_INTERVAL", "60")),
        rules_file=env.get("RULES_FILE", "/app/config/rules.yaml"),
        data_dir=env.get("DATA_DIR", "/app/data"),
        notify_to=env.get("NOTIFY_TO", "").strip(),
        notify_from=notify_from,
        notify_cooldown=int(env.get("NOTIFY_COOLDOWN", "3600")),
        full_scan_hour=int(env.get("FULL_SCAN_HOUR", "3")),
    )


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _parse_rules(items, where):
    rules = []
    for r in items or []:
        rule = Rule(
            name=str(r.get("name") or r.get("move_to")),
            move_to=str(r["move_to"]),
            senders={_norm_addr(a) for a in _as_list(r.get("from"))},
            sender_domains={_norm_domain(d) for d in _as_list(r.get("from_domain"))},
            subject_contains=[s.lower() for s in _as_list(r.get("subject_contains"))],
            spam_header=bool(r.get("spam_header", False)),
            learn=bool(r.get("learn", False)),
            mark_read=bool(r.get("mark_read", False)),
        )
        if not rule.has_conditions:
            raise ValueError(f"Правило '{rule.name}' ({where}) без условий")
        rules.append(rule)
    return rules


def load_rules(path: str, mailboxes=None) -> dict:
    """Возвращает {"boxes": {mailbox: {"rules": [Rule], "skip": set}}, "learn": LearnSpam|None}.

    Правила блока `_all` подставляются в начало списка каждого ящика из `mailboxes`
    (если список ящиков не передан — только в ящики, упомянутые в файле)."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    glob = data.pop(GLOBAL_KEY, None) or {}
    if isinstance(glob, list):
        glob = {"rules": glob}
    g_rules = _parse_rules(glob.get("rules"), GLOBAL_KEY)
    g_skip = set(_as_list(glob.get("skip_folders")))
    learn = None
    if glob.get("learn_spam"):
        ls = glob["learn_spam"] if isinstance(glob["learn_spam"], dict) else {}
        never = _as_list(ls.get("never"))
        learn = LearnSpam(
            folder=str(ls.get("folder", "Нежелательная почта")),
            never_senders={_norm_addr(x) for x in never if "@" in x.lstrip("@")},
            never_domains={_norm_domain(x) for x in never if "@" not in x.lstrip("@")},
        )
        # свои ящики и их домены никогда не считаем спамом
        for mb in mailboxes or []:
            learn.never_domains.add(mb.split("@")[-1])

    boxes = {}
    for mailbox, conf in data.items():
        if isinstance(conf, list):
            conf = {"rules": conf}
        boxes[_norm_addr(mailbox)] = {
            "rules": _parse_rules(conf.get("rules"), mailbox),
            "skip": set(_as_list(conf.get("skip_folders"))),
        }
    targets = set(boxes) | set(mailboxes or [])
    result = {}
    for mb in targets:
        own = boxes.get(mb, {"rules": [], "skip": set()})
        result[mb] = {
            "rules": g_rules + own["rules"],
            "skip": set(DEFAULT_SKIP_FOLDERS) | g_skip | own["skip"],
        }
    return {"boxes": result, "learn": learn}
