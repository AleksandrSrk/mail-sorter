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
