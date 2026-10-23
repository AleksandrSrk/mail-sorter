import textwrap

import pytest

from app import imap_utf7
from app.config import DEFAULT_SKIP_FOLDERS, load_rules, load_settings


def test_utf7_roundtrip():
    for name in ["ПУЯНГ", "Удаленные элементы", "INBOX", "A&B", "Папка & co"]:
        assert imap_utf7.decode(imap_utf7.encode(name)) == name


def test_utf7_known_value():
    assert imap_utf7.encode("Отправленные") == "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"


def write(tmp_path, text):
    p = tmp_path / "rules.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


def test_load_and_match(tmp_path):
    rules = load_rules(write(tmp_path, """
        Box@Example.ru:
          rules:
            - name: Партнёр
              from: [Partner@Mail.ru, other@gmail.com]
              move_to: ПАРТНЁР
            - name: Банк
              from_domain: "@bank.ru"
              move_to: БАНК
            - name: Дислокация
              subject_contains: [дислокац]
              move_to: ДИСЛОКАЦИЯ
          skip_folders: [Archive]
    """))
    conf = rules["boxes"]["box@example.ru"]
    r1, r2, r3 = conf["rules"]
    assert r1.matches("partner@mail.ru", "")
    assert not r1.matches("xpartner@mail.ru", "")
    assert r2.matches("noreply@bank.ru", "")
    assert not r2.matches("noreply@notbank.ru", "")
    assert r3.matches("a@b.ru", "Отчёт ДИСЛОКАЦИЯ вагонов")
    assert conf["skip"] >= set(DEFAULT_SKIP_FOLDERS) | {"Archive"}


def test_rule_without_conditions(tmp_path):
    with pytest.raises(ValueError):
        load_rules(write(tmp_path, """
            a@b.ru:
              - name: пусто
                move_to: X
        """))


def test_settings_from_env():
    s = load_settings({"IMAP_HOST": "imap.test", "MAILBOXES": '{"A@B.ru": "pw"}', "NOTIFY_FROM": "A@b.ru", "POLL_INTERVAL": "30"})
    assert s.mailboxes == {"a@b.ru": "pw"}
    assert s.notify_from == "a@b.ru"
    assert s.poll_interval == 30


def test_settings_requires_mailboxes():
    with pytest.raises(RuntimeError):
        load_settings({})


GLOBAL = """
    _all:
      rules:
        - name: Спам
          from: [Bad@Spam.ru]
          from_domain: [junk.example]
          spam_header: true
          learn: true
          move_to: Нежелательная почта
          mark_read: true
      learn_spam:
        folder: Нежелательная почта
        never: [friend@gmail.com, "@partner.ru"]
    box@our.ru:
      rules:
        - name: Партнёр
          from: [p@mail.ru]
          move_to: ПАРТНЁР
"""


def test_global_rules_apply_to_all_mailboxes(tmp_path):
    res = load_rules(write(tmp_path, GLOBAL), ["box@our.ru", "other@our2.ru"])
    boxes = res["boxes"]
    assert set(boxes) == {"box@our.ru", "other@our2.ru"}
    assert [r.name for r in boxes["box@our.ru"]["rules"]] == ["Спам", "Партнёр"]
    assert [r.name for r in boxes["other@our2.ru"]["rules"]] == ["Спам"]
    spam = boxes["other@our2.ru"]["rules"][0]
    assert spam.mark_read
    assert spam.matches("bad@spam.ru", "")
    assert spam.matches("x@junk.example", "")
    assert spam.matches("x@ok.ru", "", spam_flag=True)
    assert not spam.matches("x@ok.ru", "")
    assert spam.matches("learned@x.ru", "", learned=frozenset({"learned@x.ru"}))


def test_learn_whitelist_and_own_domains(tmp_path):
    learn = load_rules(write(tmp_path, GLOBAL), ["box@our.ru", "other@our2.ru"])["learn"]
    assert learn.folder == "Нежелательная почта"
    assert learn.allowed("spammer@x.ru")
    assert not learn.allowed("friend@gmail.com")
    assert not learn.allowed("any@partner.ru")
    assert not learn.allowed("colleague@our.ru")
    assert not learn.allowed("colleague@our2.ru")
    assert not learn.allowed("")


def test_no_global_block(tmp_path):
    res = load_rules(write(tmp_path, """
        a@b.ru:
          - name: X
            from: [x@y.ru]
            move_to: X
    """), ["a@b.ru", "c@d.ru"])
    assert res["learn"] is None
    assert res["boxes"]["c@d.ru"]["rules"] == []
