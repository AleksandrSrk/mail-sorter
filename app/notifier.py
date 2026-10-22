"""Уведомления о сбоях на почту (с ограничением частоты)."""
import json
import logging
import os
import smtplib
import time
from email.mime.text import MIMEText

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, settings):
        self.s = settings
        self.state_path = os.path.join(settings.data_dir, "notify_state.json")

    @property
    def enabled(self):
        return bool(self.s.notify_to and self.s.notify_from and self.s.notify_from in self.s.mailboxes)

    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, st):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, self.state_path)

    def send(self, key: str, subject: str, body: str, force: bool = False) -> bool:
        if not self.enabled:
            return False
        st = self._load()
        now = time.time()
        if not force and now - st.get(key, 0) < self.s.notify_cooldown:
            return False
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[mail-sorter] {subject}"
        msg["From"] = self.s.notify_from
        msg["To"] = self.s.notify_to
        try:
            with smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=60) as smtp:
                smtp.login(self.s.notify_from, self.s.mailboxes[self.s.notify_from])
                smtp.sendmail(self.s.notify_from, [self.s.notify_to], msg.as_string())
        except Exception as e:  # уведомление не должно ронять сервис
            log.error("не удалось отправить уведомление: %s", e)
            return False
        st[key] = now
        self._save(st)
        log.info("уведомление отправлено: %s", subject)
        return True
