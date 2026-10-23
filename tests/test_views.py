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
