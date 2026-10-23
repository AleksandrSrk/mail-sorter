from datetime import datetime, timedelta, timezone

from app.db import Store


def spam_rule(rules):
    return next(r for r in rules if r.learn)


def test_seed_once_and_rules_per_mailbox(store, tmp_path):
    assert not store.seed(str(tmp_path / "rules.yaml"))           # второй раз не переносит
    box = store.rules_for("box@our.ru")
    assert [r.name for r in box] == ["Спам", "Партнёр"]            # общие — первыми
    assert [r.name for r in store.rules_for("other@our2.ru")] == ["Спам"]
    spam = spam_rule(box)
    assert spam.senders == {"bad@spam.ru", "news@legit.ru", "old@spam.ru"}
    assert spam.sender_domains == {"junk.example"}
    assert spam.exempt_senders == {"friend@gmail.com"} and spam.exempt_domains == {"partner.ru"}
    assert spam.mark_read and spam.spam_header and spam.move_to == "Junk"


def test_learn_new_and_unsub_queue(store):
    assert store.learn("new@x.ru", "box@our.ru", "<mailto:u@x.ru>", "") == "added"
    assert "new@x.ru" in spam_rule(store.rules_for("box@our.ru")).senders
    q = store.pending_unsubs()
    assert len(q) == 1 and q[0]["sender"] == "new@x.ru" and q[0]["mailbox"] == "box@our.ru"
    # повторное письмо — вторая отписка не ставится
    assert store.learn("new@x.ru", "box@our.ru", "<mailto:u@x.ru>", "") is None
    assert len(store.pending_unsubs()) == 1


def test_learn_without_header_no_unsub(store):
    store.learn("plain@x.ru", "box@our.ru")
    assert store.pending_unsubs() == []


def test_learn_covered_by_domain(store):
    assert store.learn("any@junk.example", "box@our.ru", "<mailto:u@junk.example>") is None
    assert store.pending_unsubs() == []


def test_seed_address_gets_unsubscribed_on_manual_drop(store):
    # стартовый адрес (news@legit.ru), письмо от него лежит в спаме — отписка ставится
    assert store.learn("news@legit.ru", "box@our.ru", "<https://legit.ru/u>", "List-Unsubscribe=One-Click") is None
    assert [r["sender"] for r in store.pending_unsubs()] == ["news@legit.ru"]


def test_full_unsub_lifecycle(store):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.learn("sber@x.ru", "box@our.ru", "<mailto:u@x.ru>", "", now=t0)
    u = store.pending_unsubs()[0]
    store.finish_unsub(u["id"], True, "ok", now=t0)
    e = store.one("SELECT * FROM entries WHERE value='sber@x.ru'")
    assert e["status"] == "unsubscribing"
    assert "sber@x.ru" in spam_rule(store.rules_for("box@our.ru")).senders   # пока ждём — в фильтре
    rid = spam_rule(store.rules_for("box@our.ru")).id
    store.touch_seen(rid, "sber@x.ru", now=t0 + timedelta(days=3))           # ещё пишут
    assert store.expire_unsubscribing(7, now=t0 + timedelta(days=8)) == []
    assert store.expire_unsubscribing(7, now=t0 + timedelta(days=11)) == ["sber@x.ru"]
    spam = spam_rule(store.rules_for("box@our.ru"))
    assert "sber@x.ru" not in spam.senders and "sber@x.ru" in spam.exempt_senders
    assert not spam.matches("sber@x.ru", "", spam_flag=True)                 # полезные письма дойдут
    # вручную снова в спам — возвращается навсегда, без новой отписки
    assert store.learn("sber@x.ru", "box@our.ru", "<mailto:u@x.ru>", "") == "returned"
    e = store.one("SELECT * FROM entries WHERE value='sber@x.ru'")
    assert e["status"] == "active" and e["no_unsub"] == 1
    assert store.pending_unsubs() == []


def test_failed_unsub_stays_active_and_not_retried(store):
    store.learn("x@y.ru", "box@our.ru", "<https://y.ru/page>", "")
    u = store.pending_unsubs()[0]
    store.finish_unsub(u["id"], False, "ссылка без one-click")
    e = store.one("SELECT * FROM entries WHERE value='x@y.ru'")
    assert e["status"] == "active" and e["unsub_at"]
    store.learn("x@y.ru", "box@our.ru", "<https://y.ru/page>", "")
    assert store.pending_unsubs() == []


def test_removed_then_dropped_again(store):
    store.learn("r@x.ru", "box@our.ru")
    store.x("UPDATE entries SET status='removed' WHERE value='r@x.ru'")
    assert store.learn("r@x.ru", "box@our.ru") == "returned"
    assert store.one("SELECT no_unsub FROM entries WHERE value='r@x.ru'")["no_unsub"] == 0


def test_contacts(store):
    store.add_contacts(["A@Partner.ru", "a@partner.ru", "b@partner.ru", "junk"])
    by_addr, by_domain = store.contacts()
    assert by_addr == {"a@partner.ru": 2, "b@partner.ru": 1}
    assert by_domain == {"partner.ru": 3}


def test_empty_db_without_rules(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    assert s.seed(str(tmp_path / "missing.yaml"))
    assert s.rules_for("a@b.ru") == [] and s.learn_rule() is None
    assert s.learn("x@y.ru", "a@b.ru") is None


def test_unsub_once_per_mailbox(store):
    store.learn("news@legit.ru", "a@our.ru", "<mailto:u@legit.ru>")
    store.learn("news@legit.ru", "a@our.ru", "<mailto:u@legit.ru>")
    store.learn("news@legit.ru", "b@our.ru", "<mailto:u@legit.ru>")
    assert sorted(r["mailbox"] for r in store.pending_unsubs()) == ["a@our.ru", "b@our.ru"]


def test_migration_of_old_db(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE rules(id INTEGER PRIMARY KEY, mailbox TEXT NOT NULL, name TEXT NOT NULL,
        move_to TEXT NOT NULL, mark_read INTEGER NOT NULL DEFAULT 0, spam_header INTEGER NOT NULL DEFAULT 0,
        learn INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 100, enabled INTEGER NOT NULL DEFAULT 1,
        UNIQUE(mailbox, name));
        CREATE TABLE reports(id INTEGER PRIMARY KEY, sender TEXT NOT NULL, mailbox TEXT, action TEXT,
        boxes TEXT NOT NULL DEFAULT '{}', created_at TEXT, done_at TEXT);
        INSERT INTO rules(mailbox, name, move_to, learn, spam_header) VALUES('*', 'Спам', 'Junk', 1, 1);
        INSERT INTO rules(mailbox, name, move_to) VALUES('a@b.ru', 'ПУЯНГ', 'ПУЯНГ');""")
    db.commit()
    db.close()
    s = Store(str(path))
    assert s.learn_rule()["name"] == "Спам"
    assert [r["name"] for r in s.learn_rules("a@b.ru")] == ["Спам"]
    s.open_report("x@y.ru", "a@b.ru", "added")
    assert s.one("SELECT rule_id FROM reports")["rule_id"] == s.learn_rule()["id"]


def test_lookup_letters_purged_and_old_schema_migrated(store, tmp_path):
    old = datetime(2026, 10, 1, tzinfo=timezone.utc)
    lid = store.queue_lookup("a@b.ru", now=old)
    store.lookup_progress(lid, "x@our.ru", {"folders": {"INBOX": 1}, "sent": 0, "letters": [{"text": "секрет"}]})
    fresh = store.queue_lookup("c@d.ru")
    store.lookup_progress(fresh, "x@our.ru", {"folders": {}, "sent": 0, "letters": [{"text": "свежее"}]})
    store.purge_lookup_letters(days=3)
    assert "letters" not in store.get_lookup(lid)["boxes"]["x@our.ru"]
    assert store.get_lookup(lid)["boxes"]["x@our.ru"]["folders"] == {"INBOX": 1}   # цифры остаются
    assert store.get_lookup(fresh)["boxes"]["x@our.ru"]["letters"]

    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE lookups(id INTEGER PRIMARY KEY, address TEXT NOT NULL, "
               "boxes TEXT NOT NULL DEFAULT '{}', created_at TEXT, sent_at TEXT)")
    db.commit()
    db.close()
    s2 = Store(path)
    assert {"decision", "decided_at", "ai_at"} <= {r["name"] for r in s2.q("PRAGMA table_info(lookups)")}
