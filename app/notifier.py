"""Уведомления в Telegram владельцу (ошибки — с ограничением частоты, события — сразу).

Шлём напрямую через Bot API, а не через контейнер бота: ошибка дойдёт, даже если бот лежит."""
import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("notifier")
API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self, settings, transport=None):
        self.s = settings
        self.state_path = os.path.join(settings.data_dir, "notify_state.json")
        self.transport = transport or self._post

    @property
    def enabled(self):
        return bool(self.s.tg_token and self.s.tg_admin)

    def _post(self, text):
        data = json.dumps({"chat_id": self.s.tg_admin, "text": text,
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(API.format(token=self.s.tg_token), data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

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

    def send(self, key: str, subject: str, body: str = "", force: bool = False) -> bool:
        """key — для ограничения частоты одинаковых сообщений; force — слать без ограничения."""
        if not self.enabled:
            return False
        st = self._load()
        now = time.time()
        if not force and now - st.get(key, 0) < self.s.notify_cooldown:
            return False
        text = f"📬 mail-sorter: {subject}" + (f"\n\n{body}" if body else "")
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        try:
            self.transport(text)
        except Exception as e:  # уведомление не должно ронять сервис
            log.error("не удалось отправить в Telegram: %s", type(e).__name__)
            return False
        st[key] = now
        self._save(st)
        log.info("уведомление отправлено: %s", subject)
        return True
