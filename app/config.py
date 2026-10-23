"""Настройки из переменных окружения, модель правила, разбор правил из YAML (для первичного переноса в базу)."""
import json
import os
from dataclasses import dataclass, field


# Папки, из которых никогда ничего не забираем (корзина, спам, черновики).
DEFAULT_SKIP_FOLDERS = ["Удаленные элементы", "Нежелательная почта", "Черновики"]


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
    learn: bool = False         # в это правило пишет обучение (папка спама = move_to)
    mark_read: bool = False
    unsubscribe: bool = False   # отписываться от рассылок, брошенных пользователем в эту папку (спам)
    id: int = 0
    exempt_senders: set = field(default_factory=set)  # сняты с правила вручную / отписаны — не трогаем
    exempt_domains: set = field(default_factory=set)

    def matches(self, sender: str, subject: str, spam_flag: bool = False) -> bool:
        sender = _norm_addr(sender)
        domain = sender.rsplit("@", 1)[1] if "@" in sender else ""
        if sender and (sender in self.exempt_senders or domain in self.exempt_domains):
            return False
        if sender and sender in self.senders:
            return True
        if domain and domain in self.sender_domains:
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
    never_domains: set = field(default_factory=set)  # свои домены — никогда не спам

    def allowed(self, sender: str) -> bool:
        sender = _norm_addr(sender)
        if not sender or "@" not in sender:
            return False
        return sender.rsplit("@", 1)[1] not in self.never_domains


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
    notify_cooldown: int
    full_scan_hour: int
    tg_token: str
    tg_admin: int
    unsub_quiet_days: int

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "mail.db")

    @property
    def own_domains(self):
        return {mb.split("@")[-1] for mb in self.mailboxes}


def load_settings(env=os.environ) -> Settings:
    raw = env.get("MAILBOXES", "").strip()
    if not raw:
        raise RuntimeError("MAILBOXES не задан")
    mailboxes = {k.strip().lower(): v for k, v in json.loads(raw).items()}
    return Settings(
        mailboxes=mailboxes,
        imap_host=env["IMAP_HOST"],
        imap_port=int(env.get("IMAP_PORT", "993")),
        smtp_host=env.get("SMTP_HOST") or env["IMAP_HOST"],
        smtp_port=int(env.get("SMTP_PORT", "465")),
        poll_interval=int(env.get("POLL_INTERVAL", "60")),
        rules_file=env.get("RULES_FILE", "/app/config/rules.yaml"),
        data_dir=env.get("DATA_DIR", "/app/data"),
        notify_cooldown=int(env.get("NOTIFY_COOLDOWN", "3600")),
        full_scan_hour=int(env.get("FULL_SCAN_HOUR", "3")),
        tg_token=env.get("TG_BOT_TOKEN", "").strip(),
        tg_admin=int(env.get("TG_ADMIN_ID", "0") or 0),
        unsub_quiet_days=int(env.get("UNSUB_QUIET_DAYS", "7")),
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
