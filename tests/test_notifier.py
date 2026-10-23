from types import SimpleNamespace

from app.notifier import Notifier


def test_cooldown_and_force(tmp_path):
    sent = []
    s = SimpleNamespace(data_dir=str(tmp_path), tg_token="t", tg_admin=1, notify_cooldown=3600)
    n = Notifier(s, transport=sent.append)
    assert n.send("err:x", "ошибка", "trace")
    assert not n.send("err:x", "ошибка", "trace")      # та же ошибка — не чаще раза в час
    assert n.send("err:x", "ошибка", force=True)
    assert len(sent) == 2 and sent[0].startswith("📬 mail-sorter: ошибка")


def test_disabled_without_token(tmp_path):
    s = SimpleNamespace(data_dir=str(tmp_path), tg_token="", tg_admin=0, notify_cooldown=1)
    assert not Notifier(s, transport=lambda t: 1 / 0).send("k", "x")
