from app import views


def ids(rows, prefix):
    return [d for row in rows for _, d in row if d.startswith(prefix)]


def test_addresses_and_suspicious_on_top(store):
    store.add_contacts(["news@legit.ru"])                      # мы им писали
    text, rows = views.addresses(store)
    lines = text.splitlines()
    assert lines[2] == "1. news@legit.ru"
    assert "⚠️" in lines[3] and "мы писали ему 1 раз" in lines[3]
    assert len(ids(rows, "a:del:")) == 3 and len(ids(rows, "a:hide:")) == 3


def test_remove_and_hide_address(store):
    _, rows = views.addresses(store)
    eid = ids(rows, "a:del:")[0].split(":")[2]
    (text, rows), note = views.act(store, f"a:del:{eid}:0")
    assert note.startswith("Убран") and len(ids(rows, "a:del:")) == 2
    hid = ids(rows, "a:hide:")[0]
    (text, rows), note = views.act(store, hid)
    assert len(ids(rows, "a:del:")) == 1
    htext, hrows = views.hidden(store)
    assert "Скрытые: 1" in htext
    (htext, hrows), _ = views.act(store, ids(hrows, "h:dela:")[0])
    assert "Скрытых нет" in htext


def test_domains_whole_and_free(store):
    rid = store.learn_rule()["id"]
    store.add_entry(rid, "addr", "x1@mail.ru", "learned")
    text, rows = views.domains(store)
    assert "junk.example — весь домен в фильтре" in text
    # для mail.ru нет кнопки «весь домен»
    dom_id = store.one("SELECT id FROM domains WHERE domain='mail.ru'")["id"]
    assert f"d:all:{dom_id}:0" not in ids(rows, "d:all:")
    _, note = views.act(store, f"d:all:{dom_id}:0")
    assert "нельзя" in note
    spam_id = store.one("SELECT id FROM domains WHERE domain='spam.ru'")["id"]
    (_, rows), note = views.act(store, f"d:all:{spam_id}:0")
    assert "spam.ru" in {r["value"] for r in store.q("SELECT value FROM entries WHERE kind='domain'")}
    # убрать домен — снимает и домен, и его адреса
    views.act(store, f"d:del:{spam_id}:0")
    left = {r["value"] for r in store.q("SELECT value FROM entries WHERE status IN ('active','unsubscribing')")}
    assert not any(v.endswith("spam.ru") for v in left)


def test_unsubscribed_screen(store):
    store.learn("n@x.ru", "box@our.ru", "<mailto:u@x.ru>")
    u = store.pending_unsubs()[0]
    store.finish_unsub(u["id"], True, "письмо на u@x.ru")
    text, _ = views.unsubscribed(store)
    assert "n@x.ru" in text and "ждём тишину" in text and "box@our.ru" in text


def test_paging_and_garbage(store):
    rid = store.learn_rule()["id"]
    for i in range(25):
        store.add_entry(rid, "addr", f"s{i}@many.ru", "learned")
    text, rows = views.addresses(store, 0)
    assert rows[-1][-1] == ("▶️", "a:p:0:1")
    (text, rows), _ = views.act(store, "a:p:0:2")
    assert "3/3" in [t for row in rows for t, _ in row]
    assert views.act(store, "мусор") == (None, "")
    assert all(len(d.encode()) <= 64 for row in rows for _, d in row)


def test_parse_addresses():
    assert views.parse_addresses("Bad@Spam.ru, x@y.ru\nbad@spam.ru и мусор.") == ["bad@spam.ru", "x@y.ru"]
    assert views.parse_addresses("тут нет адреса") == []


def test_parse_addresses_from_copied_text():
    cases = {
        "ООО «Ромашка» <info@romashka.example>": ["info@romashka.example"],
        '"Фирма Качество" <info@quality.example>;': ["info@quality.example"],
        "mailto:klass@etp.example.ru": ["klass@etp.example.ru"],
        "e-mail: user.x2000@yandex.ru.": ["user.x2000@yandex.ru"],
        "«first.last+tag@sub.mail.example.com»": ["first.last+tag@sub.mail.example.com"],
        "(robot_1@wagon-track.example), [a-b@c.de]": ["robot_1@wagon-track.example", "a-b@c.de"],
        "Кому: Иван <ivan@x.ru>, Пётр <petr@y.ru>": ["ivan@x.ru", "petr@y.ru"],
    }
    for text, want in cases.items():
        assert views.parse_addresses(text) == want, text


def test_check_address_everywhere(store):
    bank = store.add_rule("*", "БАНК", "БАНК", learn=True, position=50)
    store.add_entry(bank, "addr", "robot@bank.ru", "seed")
    store.learn("n@x.ru", "box@our.ru", "<mailto:u@x.ru>")
    store.finish_unsub(store.pending_unsubs()[0]["id"], True, "письмо на u@x.ru")
    t = views.check_address(store, "n@x.ru")
    assert "«Спам»: адрес — 📭 в фильтре, отписались" in t and "отписка ✅ box@our.ru: письмо на u@x.ru" in t
    t = views.check_address(store, "robot@bank.ru")
    assert "«БАНК»: адрес — 🔴 в фильтре" in t and "Мы ему не писали." in t
    t = views.check_address(store, "p@mail.ru")
    assert "«Партнёр» (только box@our.ru)" in t
    t = views.check_address(store, "any@junk.example")
    assert "весь домен @junk.example" in t
    store.add_contacts(["nobody@corp.ru"])
    t = views.check_address(store, "nobody@corp.ru", own_domains={"our.ru"})
    assert "Ни в одном списке нет." in t and "писали: 1 письмо (по ящикам — после ближайшего прохода)" in t
    store.add_box_contacts(["nobody@corp.ru"] * 3, "b@our.ru")
    store.add_box_contacts(["nobody@corp.ru"] * 2, "a@our.ru")
    t = views.check_address(store, "nobody@corp.ru", own_domains={"our.ru"})
    assert "писали: 1 письмо\n  • b@our — 3\n  • a@our — 2" in t
    assert "Свой домен" in views.check_address(store, "boss@our.ru", own_domains={"our.ru"})


def test_exempt_address_under_blocked_domain(store):
    text, rows = views.check_screen(store, "good@junk.example")
    datas = [d for row in rows for _, d in row]
    assert any(d.startswith("x:rm:") for d in datas)                  # убрать весь домен
    ex = next(d for d in datas if d.startswith("x:ex:"))
    _, _, rid, addr = ex.split(":", 3)
    (text, rows), note = views.exempt(store, int(rid), addr)
    spam = store.rules_for("box@our.ru")[0]
    assert not spam.matches("good@junk.example", "") and spam.matches("other@junk.example", "")
    assert store.pending_restores("box@our.ru")[0]["needle"] == "good@junk.example"
    assert not any(d.startswith("x:ex:") for row in rows for _, d in row)  # второй раз кнопки нет
    assert views.exempt(store, int(rid), "мусор") == (None, "Устаревшая кнопка")


def test_not_spam_button_for_server_flagged_sender(store):
    text, rows = views.check_screen(store, "promo@shop.ru", own_domains={"our.ru"})
    assert "Ни в одном списке нет." in text and "серверной метке" in text
    data = rows[0][0][1]
    assert data.startswith("x:ex:") and rows[0][0][0].startswith("✅ Не спам")
    (text, rows), _ = views.exempt(store, int(data.split(":")[2]), "promo@shop.ru")
    assert not store.rules_for("box@our.ru")[0].matches("promo@shop.ru", "", spam_flag=True)
    assert rows == []
    _, rows = views.check_screen(store, "boss@our.ru", own_domains={"our.ru"})
    assert rows == []                                                       # свои — кнопка не нужна


def test_letters_plural():
    assert [views.letters(n) for n in (1, 2, 5, 11, 12, 21, 22, 25, 101)] == [
        "1 письмо", "2 письма", "5 писем", "11 писем", "12 писем", "21 письмо", "22 письма", "25 писем", "101 письмо"]


def test_check_queues_lookup_and_report_text(store):
    text, _ = views.check_with_lookup(store, "partner@corp.ru")
    assert text.endswith(views.LOOKUP_NOTE)
    views.check_with_lookup(store, "partner@corp.ru")
    assert len(store.q("SELECT * FROM lookups")) == 1                  # повторная проверка не плодит отчёты
    r = {"address": "partner@corp.ru", "missing": ["c@our.ru"], "boxes": {
        "a@our.ru": {"folders": {"INBOX": 3, "Archive": 9}, "sent": 2, "first": "2025-01-10", "last": "2026-10-20",
                     "last_sent": "2026-10-21"},
        "b@our.ru": {"folders": {}, "sent": 0, "first": "", "last": "", "last_sent": ""},
        "d@our.ru": {"folders": {"Нежелательная почта": 1}, "sent": 0, "first": "2026-10-23", "last": "2026-10-23",
                     "last_sent": ""}}}
    t = views.format_lookup(r)
    assert t.startswith("📊 partner@corp.ru — письма по ящикам\n"
                        "Он нам: 13 писем — a@our 12, d@our 1\nМы ему: 2 письма — a@our 2\n")
    assert ("📬 a@our.ru: от него 12, от нас 2\n  • Archive — 9\n  • Входящие — 3\n"
            "  от него: 2025-01-10 … 2026-10-20 · наше последнее: 2026-10-21") in t
    assert "📬 d@our.ru: от него 1\n  • Нежелательная почта — 1\n  от него: 2026-10-23" in t
    assert "В остальных ящиках (1) ничего." in t and "Не проверены (ящик недоступен): c@our.ru" in t
    empty = views.format_lookup({"address": "x@y.ru", "missing": [], "boxes": {"a@our.ru": r["boxes"]["b@our.ru"]}})
    assert "Он нам: писем нет\nМы ему: не писали" in empty
    assert "Ни в одном ящике писем от него и к нему нет." in empty


def test_lookup_buttons_and_ai(store, monkeypatch):
    lid = store.queue_lookup("partner@corp.ru")
    store.lookup_progress(lid, "a@our.ru", {"folders": {"INBOX": 1}, "sent": 0, "letters": [
        {"dir": "in", "date": "2026-10-20", "subject": "ставка", "text": "Предлагаем 1500."}]})
    r = store.get_lookup(lid)
    assert [d for row in views.lookup_buttons(r, {"our.ru"}) for _, d in row] == [
        f"l:ai:{lid}:0", f"l:spam:{lid}:0", f"l:ham:{lid}:0"]
    own = dict(r, address="boss@our.ru")
    assert [d for row in views.lookup_buttons(own, {"our.ru"}) for _, d in row] == [f"l:ai:{lid}:0", f"l:ham:{lid}:0"]
    monkeypatch.setattr(views, "summarize_correspondence", lambda addr, boxes: (None, "упёрлись в лимит Groq (429)", 1, 0))
    assert views.lookup_ai(store, lid) == ("🧠 partner@corp.ru: нейронка не ответила — упёрлись в лимит Groq (429). "
                                           "Цифры в отчёте выше актуальны.")
    assert views.lookup_buttons(store.get_lookup(lid))[0][0][1] == f"l:ai:{lid}:0"   # можно повторить
    monkeypatch.setattr(views, "summarize_correspondence", lambda addr, boxes: ("Предлагает вагоны.", None, 1, 2))
    assert views.lookup_ai(store, lid) == ("🧠 partner@corp.ru — сводка нейронкой (писем: 1, ещё 2 не влезли в лимит)"
                                           "\n\nПредлагает вагоны.")
    assert [d for row in views.lookup_buttons(store.get_lookup(lid)) for _, d in row] == [
        f"l:spam:{lid}:0", f"l:ham:{lid}:0"]                              # нейронку второй раз не жжём
    assert views.lookup_action(store, "l:spam:x:0")[1] == "Устаревшая кнопка"
