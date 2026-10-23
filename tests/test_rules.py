import pytest

from app import imap_utf7
from app.config import Rule, _parse_rules, load_settings


def test_utf7_roundtrip():
    for name in ["ПУЯНГ", "Удаленные элементы", "INBOX", "A&B", "Папка & co"]:
        assert imap_utf7.decode(imap_utf7.encode(name)) == name


def test_utf7_known_value():
    assert imap_utf7.encode("Отправленные") == "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"


def test_rule_matching():
    r = Rule(name="X", move_to="X", senders={"partner@mail.ru"}, sender_domains={"bank.ru"},
             subject_contains=["дислокац"], spam_header=True)
    assert r.matches("Partner@Mail.ru", "")
    assert not r.matches("xpartner@mail.ru", "")
    assert r.matches("noreply@bank.ru", "")
    assert not r.matches("noreply@notbank.ru", "")
    assert r.matches("a@b.ru", "Отчёт ДИСЛОКАЦИЯ вагонов")
    assert r.matches("a@b.ru", "", spam_flag=True)


def test_exempt_beats_everything():
    r = Rule(name="X", move_to="X", sender_domains={"bank.ru"}, spam_header=True,
             exempt_senders={"good@bank.ru"}, exempt_domains={"ok.ru"})
    assert not r.matches("good@bank.ru", "")
    assert not r.matches("a@ok.ru", "", spam_flag=True)
    assert r.matches("other@bank.ru", "")


def test_rule_without_conditions():
    with pytest.raises(ValueError):
        _parse_rules([{"name": "пусто", "move_to": "X"}], "a@b.ru")


def test_settings_from_env():
    s = load_settings({"IMAP_HOST": "imap.test", "MAILBOXES": '{"A@B.ru": "pw"}', "POLL_INTERVAL": "30",
                       "TG_BOT_TOKEN": "t", "TG_ADMIN_ID": "42", "DATA_DIR": "/x"})
    assert s.mailboxes == {"a@b.ru": "pw"}
    assert s.poll_interval == 30
    assert s.tg_admin == 42 and s.tg_token == "t"
    assert s.db_path == "/x/mail.db"
    assert s.own_domains == {"b.ru"}
    assert s.unsub_quiet_days == 7


def test_settings_requires_mailboxes():
    with pytest.raises(RuntimeError):
        load_settings({})
