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
        self.searches = []
        self.stores = []

    def capability(self):
        return "OK", [b"IMAP4rev1 MOVE"]

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{n}"'.encode() for n in self.f]

    def status(self, q, _):
        n = q.strip('"')
        return "OK", [f'"{n}" (UIDNEXT {self.next[n]} UIDVALIDITY {self.f[n]["validity"]})'.encode()]

    def select(self, q, readonly=False):
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
        if cmd == "SEARCH" and args[1] in ("FROM", "TO"):
            needle = args[2].strip('"').lower()
            self.searches.append((self.sel, needle))
            pos = 0 if args[1] == "FROM" else 1        # в «Отправленных» адресат лежит в поле темы
            return "OK", [b" ".join(str(u).encode() for u in sorted(msgs) if needle in msgs[u][pos].lower())]
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
                if "BODY.PEEK[]" in args[1]:
                    hdr += f"Текст письма от {fr}: предлагаем услуги.\r\n\r\n-- \r\nподпись"
                out.append((f"{u} (UID {u} BODY[...] {{1}}".encode(), hdr.encode()))
                out.append(b")")
            return "OK", out
        if cmd == "STORE":
            self.stores.append((self.sel, args[0], args[2]))
            return "OK", []
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


def test_new_address_sweeps_old_letters(store):
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("old@veles.ru", "a", False), 2: ("ok@y.ru", "b", False)}},
                     "Archive": {"validity": 1, "msgs": {1: ("x@newdom.ru", "c", False)}},
                     "Junk": {"validity": 1, "msgs": {}}})
    s = make(fake, store)
    assert s.run_once(full=True)[0] == 0
    assert s.run_once() == (0, []) and fake.searches == []          # ничего нового — не ищем
    rid = store.learn_rule()["id"]
    store.add_entry(rid, "addr", "old@veles.ru", "learned")          # выучили в другом ящике
    store.add_entry(rid, "domain", "newdom.ru", "bot")               # ➕ весь домен в боте
    moved, _ = s.run_once()
    assert moved == 2                                                # старые письма уехали без ночного прохода
    assert sorted(fake.f["INBOX"]["msgs"]) == [2] and fake.f["Archive"]["msgs"] == {}
    assert {n for _, n in fake.searches} == {"old@veles.ru", "@newdom.ru"}
    fake.searches.clear()
    assert s.run_once()[0] == 0 and fake.searches == []             # второй раз по тем же не ищем


def test_returned_address_is_swept_again(store):
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {}}, "Junk": {"validity": 1, "msgs": {}}})
    s = make(fake, store)
    s.run_once(full=True)
    store.x("UPDATE entries SET status='removed' WHERE value='bad@spam.ru'")   # «не спам» в боте
    s.run_once()
    fake.f["INBOX"]["msgs"][1] = ("bad@spam.ru", "old", False)                # письмо уже лежит
    fake.next["INBOX"] = 2
    s.run_once()                                                              # пришло как новое — не спам
    assert 1 in fake.f["INBOX"]["msgs"]
    store.x("UPDATE entries SET status='active' WHERE value='bad@spam.ru'")    # вернули в фильтр
    assert s.run_once()[0] == 1                                               # старое письмо найдено заново


def add_folder_rules(store):
    bank = store.add_rule("*", "БАНК", "БАНК", learn=True, position=50)
    disl = store.add_rule("*", "ДИСЛОКАЦИЯ", "ДИСЛОКАЦИЯ", learn=True, position=60)
    return bank, disl


def test_learn_into_bank_moves_everywhere_unread(store):
    add_folder_rules(store)
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("robot@bank.ru", "выписка", False),
                                                       2: ("ok@y.ru", "b", False)}},
                     "Archive": {"validity": 1, "msgs": {1: ("robot@bank.ru", "старая", False)}},
                     "БАНК": {"validity": 1, "msgs": {}}, "ДИСЛОКАЦИЯ": {"validity": 1, "msgs": {}},
                     "Junk": {"validity": 1, "msgs": {}}})
    s = make(fake, store)
    s.run_once(full=True)
    # пользователь положил письмо банка в БАНК
    fake.f["БАНК"]["msgs"][1] = fake.f["INBOX"]["msgs"].pop(1)
    fake.next["БАНК"] = 2
    moved, events = s.run_once()
    assert events == [("robot@bank.ru", "added")]
    assert moved == 1 and fake.f["Archive"]["msgs"] == {}         # старое из архива — тоже в БАНК
    assert sorted(fake.f["БАНК"]["msgs"]) == [1, 2]
    assert not any("Seen" in flags for _, _, flags in fake.stores)  # прочитанными не помечаем
    assert store.pending_unsubs() == []                            # никаких отписок для БАНКА
    # своё же перемещение в БАНК не считается обучением
    fake.f["INBOX"]["msgs"][3] = ("robot@bank.ru", "новая", False)
    fake.next["INBOX"] = 4
    assert s.run_once() == (1, [])


def test_last_word_wins_between_folders(store):
    bank, _ = add_folder_rules(store)
    store.learn("x@shop.ru", "me@our.ru")                          # в спаме
    assert store.learn("x@shop.ru", "me@our.ru", rule_id=bank) == "added"
    rules = {r.name: r for r in store.rules_for("me@our.ru")}
    assert "x@shop.ru" not in rules["Спам"].senders and "x@shop.ru" in rules["БАНК"].senders
    assert store.learn("x@shop.ru", "me@our.ru") == "returned"     # передумал — снова спам
    rules = {r.name: r for r in store.rules_for("me@our.ru")}
    assert "x@shop.ru" in rules["Спам"].senders and "x@shop.ru" not in rules["БАНК"].senders


def test_own_domain_not_learned_but_manual_works(store):
    _, disl = add_folder_rules(store)
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("disp@our.ru", "дисл", False)}},
                     "ДИСЛОКАЦИЯ": {"validity": 1, "msgs": {1: ("boss@our.ru", "x", False)}},
                     "БАНК": {"validity": 1, "msgs": {}}, "Junk": {"validity": 1, "msgs": {}}})
    s = make(fake, store)
    assert s.run_once(full=True) == (0, [])                       # boss@our.ru не выучен
    store.add_entry(disl, "addr", "disp@our.ru", "manual")         # disp@ — руками
    assert s.run_once()[0] == 1 and 1 not in fake.f["INBOX"]["msgs"]


def test_mailbox_rule_beats_learned_folder(store):
    _, disl = add_folder_rules(store)
    store.add_entry(disl, "addr", "p@mail.ru", "learned")          # контрагент ПУЯНГа выучен в ДИСЛОКАЦИЮ
    rules = store.rules_for("box@our.ru")
    assert [r.name for r in rules] == ["Спам", "Партнёр", "БАНК", "ДИСЛОКАЦИЯ"]


def test_existing_folder_content_is_learned_on_start(store):
    add_folder_rules(store)
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("robot@disl.ru", "a", False)}},
                     "ДИСЛОКАЦИЯ": {"validity": 1, "msgs": {1: ("robot@disl.ru", "old", False)}},
                     "БАНК": {"validity": 1, "msgs": {}}, "Junk": {"validity": 1, "msgs": {}}})
    moved, events = make(fake, store).run_once(full=True)
    assert events == [("robot@disl.ru", "added")] and moved == 1


def test_restore_button_returns_letters_to_inbox(store):
    from app import views
    fake = FakeIMAP({"INBOX": {"validity": 1, "msgs": {1: ("bad@spam.ru", "a", False)}},
                     "Junk": {"validity": 1, "msgs": {}}})
    s = make(fake, store)
    s.run_once(full=True)                                              # уехало в спам
    fake.f["Junk"]["msgs"][9] = ("other@spam.ru", "x", False)           # чужое письмо в спаме — не трогаем
    fake.next["Junk"] = 10
    s.run_once()
    text, rows = views.check_screen(store, "bad@spam.ru")
    data = rows[0][0][1]
    assert data.startswith("x:rm:") and "«Спам» → во входящие" in rows[0][0][0]
    (text, rows), note = views.restore(store, int(data.split(":")[2]))
    assert "⚪ снят с фильтра" in text and rows == []                 # кнопки больше нет
    before = len(fake.stores)
    s.run_once()
    assert [m[0] for m in fake.f["INBOX"]["msgs"].values()] == ["bad@spam.ru"]
    assert [m[0] for m in fake.f["Junk"]["msgs"].values()] == ["other@spam.ru"]
    assert fake.stores[before:] == []                                  # флаги при возврате не трогаем
    s.run_once()                                                       # во входящих и остаётся
    assert [m[0] for m in fake.f["INBOX"]["msgs"].values()] == ["bad@spam.ru"]
    r = store.ready_restores(["me@our.ru"])
    assert len(r) == 1 and r[0]["boxes"] == {"me@our.ru": 1}
