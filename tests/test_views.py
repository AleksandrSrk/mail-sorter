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
    assert "Ни в одном списке нет." in t and "писали: 1 раз" in t
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
