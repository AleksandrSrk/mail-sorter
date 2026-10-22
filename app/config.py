"""Загрузка настроек из переменных окружения и правил из YAML."""
import json
import os
from dataclasses import dataclass, field

import yaml

# Папки, из которых никогда ничего не забираем (корзина, спам, черновики).
DEFAULT_SKIP_FOLDERS = ["Удаленные элементы", "Нежелательная почта", "Черновики"]


@dataclass
class Rule:
    name: str
    move_to: str
    senders: set = field(default_factory=set)        # точные адреса
    sender_domains: set = field(default_factory=set)  # домены (без @)
    subject_contains: list = field(default_factory=list)
    mark_read: bool = False

    def matches(self, sender: str, subject: str) -> bool:
        sender = (sender or "").strip().lower()
        if sender and sender in self.senders:
            return True
        if sender and "@" in sender and sender.rsplit("@", 1)[1] in self.sender_domains:
            return True
        subj = (subject or "").lower()
        return any(s in subj for s in self.subject_contains)

    @property
    def has_sender_criteria(self) -> bool:
        return bool(self.senders or self.sender_domains)


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
    )


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def load_rules(path: str) -> dict:
    """Возвращает {mailbox: {"rules": [Rule], "skip": set(папок)}}."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    result = {}
    for mailbox, conf in data.items():
        if isinstance(conf, list):
            conf = {"rules": conf}
        rules = []
        for r in conf.get("rules", []) or []:
            rule = Rule(
                name=str(r.get("name") or r.get("move_to")),
                move_to=str(r["move_to"]),
                senders={a.strip().lower() for a in _as_list(r.get("from"))},
                sender_domains={d.strip().lower().lstrip("@") for d in _as_list(r.get("from_domain"))},
                subject_contains=[s.lower() for s in _as_list(r.get("subject_contains"))],
                mark_read=bool(r.get("mark_read", False)),
            )
            if not (rule.has_sender_criteria or rule.subject_contains):
                raise ValueError(f"Правило '{rule.name}' ({mailbox}) без условий")
            rules.append(rule)
        skip = set(DEFAULT_SKIP_FOLDERS) | set(_as_list(conf.get("skip_folders")))
        result[mailbox.strip().lower()] = {"rules": rules, "skip": skip}
    return result
