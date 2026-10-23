"""Инкрементальный обход на фейковом IMAP."""
from app.config import LearnSpam, Rule
from app.sorter import MailboxSorter, parse_status
from app.state import LearnedSpam


def test_parse_status():
    assert parse_status(b'"INBOX" (UIDNEXT 120 UIDVALIDITY 777)') == (120, 777)
    assert parse_status(b'"INBOX" (UIDVALIDITY 5 UIDNEXT 9)') == (9, 5)
    assert parse_status(b"") == (None, None)


class FakeIMAP:
    """Папки: name -> {"validity": int, "msgs": {uid: (from, subject, spamflag)}}"""

    def __init__(self, folders):
        self.f = folders
        self.sel = None
        self.next = {n: max(d["msgs"] or {0: 0}) + 1 for n, d in folders.items()}
        self.fetched = []

    def capability(self):
        return "OK", [b"IMAP4rev1 MOVE"]

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{n}"'.encode() for n in self.f]

    def status(self, q, _):
        n = q.strip('"')
        return "OK", [f'"{n}" (UIDNEXT {self.next[n]} UIDVALIDITY {self.f[n]["validity"]})'.encode()]

    def select(self, q):
        self.sel = q.strip('"')
        return "OK", [b"1"]

    def close(self):
        self.sel = None

    def create(self, q):
        n = q.strip('"')
        self.f[n] = {"validity": 1, "msgs": {}}
        self.next[n] = 1
        return "OK", []

    def subscribe(self, q):
        return "OK", []

    def logout(self):
        pass

    def uid(self, cmd, *args):
        msgs = self.f[self.sel]["msgs"]
        if cmd == "SEARCH":
            lo = int(args[2].split(":")[0])
            return "OK", [b" ".join(str(u).encode() for u in sorted(msgs) if u >= lo)]
        if cmd == "FETCH":
            out = []
            for u in args[0].split(","):
                u = int(u)
                self.fetched.append((self.sel, u))
                fr, subj, flag = msgs[u]
                hdr = f"From: {fr}\r\nSubject: {subj}\r\n" + ("X-Spam-Flag: YES\r\n" if flag else "") + "\r\n"
                out.append((f"{u} (UID {u} BODY[...] {{1}}".encode(), hdr.encode()))
                out.append(b")")
            return "OK", out
        if cmd == "MOVE":
            dest = args[1].strip('"')
            for u in args[0].split(","):
                m = msgs.pop(int(u))
                self.f[dest]["msgs"][self.next[dest]] = m
                self.next[dest] += 1
            return "OK", []
        return "OK", []


def make(fake, tmp_path, state=None):
    rules = [Rule(name="Спам", move_to="Junk", senders={"bad@x.ru"}, spam_header=True, learn=True, mark_read=True)]
    s = MailboxSorter("h", 993, "me@our.ru", "pw", rules, {"Trash"}, state if state is not None else {},
                      learn=LearnSpam(folder="Junk", never_domains={"our.ru"}), learned=LearnedSpam(str(tmp_path)))
    s._connect = lambda: (setattr(s, "caps", {"MOVE"}), fake)[1]
    return s


def test_incremental_and_learning(tmp_path):
    fake = FakeIMAP({
        "INBOX": {"validity": 1, "msgs": {1: ("bad@x.ru", "a", False), 2: ("ok@y.ru", "b", False),
                                           3: ("z@z.ru", "c", True), 4: ("later@s.ru", "d", False)}},
        "Junk": {"validity": 1, "msgs": {1: ("old@spam.ru", "x", False), 2: ("boss@our.ru", "x", False)}},
        "Trash": {"validity": 1, "msgs": {1: ("bad@x.ru", "a", False)}},
    })
    s = make(fake, tmp_path)
    moved, new = s.run_once(full=True)
    assert moved == 2                                  # bad@x.ru + спам-флаг
    assert new == ["old@spam.ru"]                      # свой домен не обучаем
    assert sorted(fake.f["INBOX"]["msgs"]) == [2, 4]
    assert 1 in fake.f["Trash"]["msgs"]                # Trash в skip

    # второй проход без новых писем — ничего не фетчим
    fake.fetched.clear()
    assert s.run_once() == (0, [])
    assert fake.fetched == []

    # пользователь кинул later@s.ru в Junk руками + пришло новое письмо от него
    fake.f["Junk"]["msgs"][fake.next["Junk"]] = fake.f["INBOX"]["msgs"].pop(4)
    fake.next["Junk"] += 1
    fake.f["INBOX"]["msgs"][5] = ("later@s.ru", "e", False)
    fake.next["INBOX"] = 6
    fake.fetched.clear()
    moved, new = s.run_once()
    assert new == ["later@s.ru"]
    assert moved == 1
    assert ("INBOX", 2) not in fake.fetched            # старые письма не перечитываем
    assert sorted(fake.f["INBOX"]["msgs"]) == [2]
    assert "later@s.ru" in open(tmp_path / "learned_spam.txt").read()


def test_uidvalidity_change_rescans(tmp_path):
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("ok@y.ru", "a", False)}},
                     "Junk": {"validity": 1, "msgs": {}}})
    state = {}
    s = make(fake, tmp_path, state)
    s.run_once(full=True)
    fake.f["INBOX"] = {"validity": 2, "msgs": {1: ("bad@x.ru", "a", False)}}
    moved, _ = s.run_once()
    assert moved == 1
