"""Состояние между проходами: последний обработанный UID по папкам и обученный спам-список."""
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


class LearnedSpam:
    """data/learned_spam.txt — по адресу на строку. Можно править руками (строки с # игнорируются)."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "learned_spam.txt")
        self._items = set()
        self.reload()

    def reload(self):
        items = set()
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip().lower()
                    if line:
                        items.add(line)
        except OSError:
            pass
        self._items = items

    @property
    def items(self) -> frozenset:
        return frozenset(self._items)

    def add(self, addresses) -> list:
        new = sorted({a.strip().lower() for a in addresses if a} - self._items)
        if new:
            with _lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    for a in new:
                        f.write(a + "\n")
            self._items.update(new)
        return new
