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
