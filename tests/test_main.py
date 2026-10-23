import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.main as main


def test_process_unsubs(store, monkeypatch):
    sent = []
    notifier = SimpleNamespace(send=lambda key, subj, body="", force=False: sent.append(subj))
    settings = SimpleNamespace(unsub_quiet_days=7)
    monkeypatch.setattr(main, "unsubscribe", lambda row, s: (True, "письмо на u@x.ru"))
    store.learn("n@x.ru", "box@our.ru", "<mailto:u@x.ru>")
    main.process_unsubs(store, settings, notifier, logging.getLogger("t"))
    assert sent == ["📭 отписался от n@x.ru"]
    assert store.pending_unsubs() == []
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    store.x("UPDATE entries SET unsub_at=? WHERE value='n@x.ru'", old)
    main.process_unsubs(store, settings, notifier, logging.getLogger("t"))
    assert sent[-1].startswith("🔕 n@x.ru")


def test_report_after_all_boxes(store, monkeypatch):
    from tests.test_sorter import FakeIMAP
    from app.sorter import MailboxSorter

    def box(login, folders):
        fake = FakeIMAP(folders)
        s = MailboxSorter("h", 993, login, "pw", store, {}, own_domains={"our.ru"})
        s._connect = lambda: (setattr(s, "caps", {"MOVE"}), fake)[1]
        return s, fake

    a, fa = box("a@our.ru", {"INBOX": {"validity": 1, "msgs": {}},
                             "Archive": {"validity": 1, "msgs": {1: ("x@spammer.ru", "1", False),
                                                                  2: ("x@spammer.ru", "2", False)}},
                             "Junk": {"validity": 1, "msgs": {}}})
    b, fb = box("b@our.ru", {"INBOX": {"validity": 1, "msgs": {1: ("x@spammer.ru", "3", False)}},
                             "Junk": {"validity": 1, "msgs": {}}})
    c, fc = box("c@our.ru", {"INBOX": {"validity": 1, "msgs": {}}, "Junk": {"validity": 1, "msgs": {}}})
    for s in (a, b, c):
        s.run_once(full=True)
    # пользователь кинул письмо x@spammer.ru в спам ящика b
    fb.f["Junk"]["msgs"][1] = ("x@spammer.ru", "drop", False, "<mailto:u@spammer.ru>")
    fb.next["Junk"] = 2
    monkeypatch.setattr(main, "unsubscribe", lambda row, s: (True, "письмо на u@spammer.ru"))
    sent = []
    notifier = SimpleNamespace(send=lambda key, subj, body="", force=False: sent.append(subj) or True, enabled=True)
    settings = SimpleNamespace(unsub_quiet_days=7, mailboxes={"a@our.ru": 1, "b@our.ru": 1, "c@our.ru": 1})
    log = logging.getLogger("t")

    def cycle():
        for s in (a, b, c):
            s.run_once()
        main.process_unsubs(store, settings, notifier, log)
        main.send_reports(store, settings, notifier, log)

    cycle()                       # a прошёл раньше, чем b выучил адрес — отчёт ещё не готов
    assert sent == [] and fb.f["INBOX"]["msgs"] == {}
    cycle()                       # a дочесал старую почту — сводка
    assert len(sent) == 1 and fa.f["Archive"]["msgs"] == {}
    text = sent[0]
    assert "🚫 В спам-фильтр добавлен: x@spammer.ru" in text and "Кинули в спам в ящике: b@our.ru" in text
    assert "перенесены в спам: 3" in text and "a@our.ru — 2" in text and "b@our.ru — 1" in text
    assert "В остальных 1 ящиках" in text and "✅ b@our.ru — письмо на u@spammer.ru" in text
    cycle()
    assert len(sent) == 1         # второй раз не шлём


def test_report_text_variants():
    base = {"sender": "x@y.ru", "mailbox": "b@our.ru", "boxes": {"a@our.ru": 0, "b@our.ru": 0}, "missing": []}
    t = main.format_report(dict(base, action="added", unsubs=[], no_unsub=False))
    assert "ни в одном из 2 ящиков" in t and "нет заголовка" in t
    t = main.format_report(dict(base, action="returned", unsubs=[], no_unsub=True, missing=["c@our.ru"]))
    assert t.startswith("↩️") and "навсегда" in t and "c@our.ru" in t


def test_report_text_folder():
    r = {"sender": "robot@bank.ru", "mailbox": "b@our.ru", "boxes": {"a@our.ru": 2, "b@our.ru": 0},
         "missing": [], "action": "added", "unsubs": [], "no_unsub": False, "folder": "БАНК", "spam": False}
    t = main.format_report(r)
    assert t.startswith("📁 БАНК: добавлен robot@bank.ru") and "Положили в «БАНК» в ящике: b@our.ru" in t
    assert "перенесены в «БАНК»: 2" in t and "Отписка" not in t


def test_restore_text():
    r = {"needle": "x@y.ru", "rule_name": "Спам", "folder": "Junk", "boxes": {"a@our.ru": 3, "b@our.ru": 0},
         "missing": []}
    t = main.format_restore(r)
    assert t.startswith("↩️ x@y.ru убран из «Спам»") and "возвращено писем: 3" in t and "b@our.ru" not in t
    t = main.format_restore(dict(r, boxes={"a@our.ru": 0}))
    assert "возвращать нечего" in t


def _boxes(store, spec):
    from tests.test_sorter import FakeIMAP
    from app.sorter import MailboxSorter
    out = []
    for login, folders in spec:
        fake = FakeIMAP(folders)
        s = MailboxSorter("h", 993, login, "pw", store, {}, own_domains={"our.ru"})
        s._connect = (lambda s_, f_: lambda: (setattr(s_, "caps", {"MOVE"}), f_)[1])(s, fake)
        out.append((s, fake))
    return out


def test_server_spam_notice_and_decisions(store, monkeypatch):
    from app import views
    store.add_contacts(["friend@corp.ru"])
    (a, fa), (b, fb) = _boxes(store, [
        ("a@our.ru", {"INBOX": {"validity": 1, "msgs": {1: ("promo@shop.ru", "Скидки", True, "<mailto:u@shop.ru>"),
                                                         2: ("friend@corp.ru", "Счёт", True)}},
                      "Junk": {"validity": 1, "msgs": {}}, "Отправленные": {"validity": 1, "msgs": {}}}),
        ("b@our.ru", {"INBOX": {"validity": 1, "msgs": {}},
                      "Archive": {"validity": 1, "msgs": {1: ("promo@shop.ru", "Старое", False)}},
                      "Junk": {"validity": 1, "msgs": {}},
                      "Отправленные": {"validity": 1, "msgs": {1: ("me@our.ru", "promo@shop.ru", False)}}}),
    ])
    sent = []
    notifier = SimpleNamespace(enabled=True, send=lambda key, subj, body="", force=False, buttons=None:
                               sent.append((subj, buttons)) or True)
    settings = SimpleNamespace(unsub_quiet_days=7, mailboxes={"a@our.ru": 1, "b@our.ru": 1})
    log = logging.getLogger("t")
    a.run_once(full=True)
    b.run_once(full=True)
    assert 2 in fa.f["INBOX"]["msgs"]                         # friend@ — мы ему писали, метка сервера не трогает
    assert [m[0] for m in fa.f["Junk"]["msgs"].values()] == ["promo@shop.ru"]
    main.send_notices(store, settings, notifier, log)
    assert len(sent) == 1
    text, buttons = sent[0]
    assert "пометил письмо как спам" in text and "От: promo@shop.ru" in text and "Тема: Скидки" in text
    assert "Кратко (по 2 последним письмам): Текст письма от promo@shop.ru: предлагаем услуги." in text  # подпись отрезана
    assert store.one("SELECT texts FROM notices")["texts"].count('"subject"') == 2   # из обоих ящиков
    assert "a@our — 1 (в спаме 1)" in text and "b@our — 1" in text            # письма по ящикам
    assert "мы ему писали:" in text and "b@our — 1" in text                     # и переписка
    assert [d for _, d in buttons[0]] == ["n:keep:1:0", "n:rel:1:0"]
    main.send_notices(store, settings, notifier, log)
    assert len(sent) == 1                                     # повторно не шлём

    # «Не спам» — исключение и возврат во входящие
    line, note = views.notice_action(store, "n:rel:1:0")
    assert line.startswith("✅ Решение: не спам")
    a.run_once()
    assert [m[0] for m in fa.f["INBOX"]["msgs"].values()].count("promo@shop.ru") == 1
    assert fa.f["Junk"]["msgs"] == {}
    assert views.notice_action(store, "n:keep:1:0")[1] == "Уже решено"
    fa.f["INBOX"]["msgs"][9] = ("promo@shop.ru", "Ещё", True)  # новые письма по метке больше не уходят
    fa.next["INBOX"] = 10
    a.run_once()
    assert 9 in fa.f["INBOX"]["msgs"]


def test_server_spam_keep_learns_everywhere(store, monkeypatch):
    from app import views
    (a, fa), (b, fb) = _boxes(store, [
        ("a@our.ru", {"INBOX": {"validity": 1, "msgs": {1: ("promo@shop.ru", "Скидки", True, "<mailto:u@shop.ru>")}},
                      "Junk": {"validity": 1, "msgs": {}}}),
        ("b@our.ru", {"INBOX": {"validity": 1, "msgs": {}},
                      "Archive": {"validity": 1, "msgs": {1: ("promo@shop.ru", "Старое", False)}},
                      "Junk": {"validity": 1, "msgs": {}}}),
    ])
    a.run_once(full=True)
    b.run_once(full=True)
    assert 1 in fb.f["Archive"]["msgs"]                       # без метки — пока не трогаем
    nid = store.ready_notices(["a@our.ru", "b@our.ru"])[0]["id"]
    line, _ = views.notice_action(store, f"n:keep:{nid}:0")
    assert line.startswith("🚫 Решение: спам")
    assert [r["sender"] for r in store.pending_unsubs()] == ["promo@shop.ru"]   # решение пользователя — отписываемся
    b.run_once()
    assert fb.f["Archive"]["msgs"] == {}                      # догоняющий поиск унёс старое письмо в спам
    a.run_once()
    monkeypatch.setattr(main, "unsubscribe", lambda row, s: (True, "письмо на u@shop.ru"))
    main.process_unsubs(store, SimpleNamespace(unsub_quiet_days=7), SimpleNamespace(send=lambda *a, **k: True),
                        logging.getLogger("t"))
    r = store.ready_reports(["a@our.ru", "b@our.ru"])
    assert r and r[0]["sender"] == "promo@shop.ru" and r[0]["boxes"] == {"a@our.ru": 0, "b@our.ru": 1}
    assert r[0]["unsubs"][0]["status"] == "done"


def test_lookup_report_after_all_boxes(store):
    (a, fa), (b, fb) = _boxes(store, [
        ("a@our.ru", {"INBOX": {"validity": 1, "msgs": {1: ("partner@corp.ru", "Счёт", False)}},
                      "Отправленные": {"validity": 1, "msgs": {1: ("me@our.ru", "partner@corp.ru", False)}}}),
        ("b@our.ru", {"INBOX": {"validity": 1, "msgs": {}}}),
    ])
    a.run_once(full=True)
    b.run_once(full=True)
    store.queue_lookup("partner@corp.ru")
    sent = []
    notifier = SimpleNamespace(enabled=True, send=lambda key, subj, body="", force=False, buttons=None:
                               sent.append(subj) or True)
    settings = SimpleNamespace(mailboxes={"a@our.ru": 1, "b@our.ru": 1})
    a.run_once()
    main.send_lookups(store, settings, notifier, logging.getLogger("t"))
    assert sent == []                                         # ждём второй ящик
    b.run_once()
    main.send_lookups(store, settings, notifier, logging.getLogger("t"))
    assert len(sent) == 1 and "📬 a@our.ru: от него 1, от нас 1\n  • Входящие — 1" in sent[0]
    assert "В остальных ящиках (1) ничего." in sent[0]
    main.send_lookups(store, settings, notifier, logging.getLogger("t"))
    assert len(sent) == 1
