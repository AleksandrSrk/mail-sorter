"""Инкрементальный обход на фейковом IMAP."""
from app.sorter import MailboxSorter, parse_status


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
                fr, subj, flag = msgs[u][:3]
                lu = msgs[u][3] if len(msgs[u]) > 3 else ""
                hdr = (f"From: {fr}\r\nSubject: {subj}\r\nTo: {subj}\r\n" + ("X-Spam-Flag: YES\r\n" if flag else "")
                       + (f"List-Unsubscribe: {lu}\r\n" if lu else "") + "\r\n")
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


def make(fake, store, state=None):
    s = MailboxSorter("h", 993, "me@our.ru", "pw", store, state if state is not None else {}, own_domains={"our.ru"})
    s._connect = lambda: (setattr(s, "caps", {"MOVE"}), fake)[1]
    return s


def test_incremental_and_learning(store, tmp_path):
    fake = FakeIMAP({
        "INBOX": {"validity": 1, "msgs": {1: ("bad@spam.ru", "a", False), 2: ("ok@y.ru", "b", False),
                                           3: ("z@z.ru", "c", True), 4: ("later@s.ru", "d", False)}},
        "Junk": {"validity": 1, "msgs": {1: ("fresh@spam.ru", "x", False, "<mailto:u@spam.ru>"),
                                          2: ("boss@our.ru", "x", False)}},
        "Удаленные элементы": {"validity": 1, "msgs": {1: ("bad@spam.ru", "a", False)}},
        "Отправленные": {"validity": 1, "msgs": {1: ("me@our.ru", "client@corp.ru", False)}},
    })
    s = make(fake, store)
    moved, new = s.run_once(full=True)
    assert moved == 2                                  # bad@spam.ru + спам-флаг
    assert new == [("fresh@spam.ru", "added")]         # свой домен не обучаем
    assert [r["sender"] for r in store.pending_unsubs()] == ["fresh@spam.ru"]
    assert sorted(fake.f["INBOX"]["msgs"]) == [2, 4]
    assert 1 in fake.f["Удаленные элементы"]["msgs"]  # корзину не трогаем
    assert store.contacts()[0] == {"client@corp.ru": 1}
    seen = store.one("SELECT last_seen_at FROM entries WHERE value='bad@spam.ru'")["last_seen_at"]
    assert seen

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
    assert new == [("later@s.ru", "added")]
    assert moved == 1
    assert ("INBOX", 2) not in fake.fetched            # старые письма не перечитываем
    assert sorted(fake.f["INBOX"]["msgs"]) == [2]
    assert store.one("SELECT source FROM entries WHERE value='later@s.ru'")["source"] == "learned"


def test_uidvalidity_change_rescans(store):
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("ok@y.ru", "a", False)}},
                     "Junk": {"validity": 1, "msgs": {}}})
    state = {}
    s = make(fake, store, state)
    s.run_once(full=True)
    fake.f["INBOX"] = {"validity": 2, "msgs": {1: ("bad@spam.ru", "a", False)}}
    moved, _ = s.run_once()
    assert moved == 1


def test_rules_reloaded_each_run(store):
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("x@new.ru", "a", False)}},
                     "Junk": {"validity": 1, "msgs": {}}})
    s = make(fake, store)
    assert s.run_once(full=True)[0] == 0
    rid = store.learn_rule()["id"]
    store.add_entry(rid, "addr", "x@new.ru", "bot")                    # добавили через бота
    assert s.run_once(full=True)[0] == 1


def test_own_domain_never_spam_even_with_flag(store):
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("colleague@our.ru", "a", True)}},
                     "Junk": {"validity": 1, "msgs": {}}})
    assert make(fake, store).run_once(full=True)[0] == 0
