"""Состояние между проходами: последний обработанный UID по папкам ящиков."""
import json
import os
import threading

_lock = threading.Lock()


class StateStore:
    """data/state.json: {"boxes": {mailbox: {raw_folder: [uidvalidity, last_uid]}}, "last_full_day": "..."}"""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "state.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("boxes", {})

    def box(self, mailbox) -> dict:
        return self.data["boxes"].setdefault(mailbox, {})

    def save(self):
        with _lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
