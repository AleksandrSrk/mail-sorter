from types import SimpleNamespace

import pytest

from app import unsub

S = SimpleNamespace(mailboxes={"box@our.ru": "pw"}, smtp_host="smtp", smtp_port=465)


def row(header, post="", mailbox="box@our.ru"):
    return {"header": header, "post": post, "mailbox": mailbox}


def test_parse_header():
    m, h = unsub.parse_header("<mailto:u@x.ru?subject=stop>, <https://x.ru/u?id=1>, <http://bad.ru/u>")
    assert m == ["mailto:u@x.ru?subject=stop"] and h == ["https://x.ru/u?id=1"]


def test_parse_mailto():
    assert unsub.parse_mailto("mailto:u%40x.ru?subject=Unsub%20me&body=bye") == ("u@x.ru", "Unsub me", "bye")
    assert unsub.parse_mailto("mailto:u@x.ru") == ("u@x.ru", "unsubscribe", "unsubscribe")


def test_both_methods():
    calls = []
    ok, res = unsub.unsubscribe(row("<mailto:u@x.ru>, <https://x.ru/u>", "List-Unsubscribe=One-Click"), S,
                                post=lambda url: calls.append(url) or "HTTP 200",
                                mail=lambda uri, mb, pw, h, p: calls.append(uri) or "письмо на u@x.ru")
    assert ok and calls == ["mailto:u@x.ru", "https://x.ru/u"]
    assert "письмо на u@x.ru" in res and "HTTP 200" in res


def test_https_without_one_click_is_skipped():
    ok, res = unsub.unsubscribe(row("<https://x.ru/page>"), S, post=lambda u: pytest.fail("нельзя"),
                                mail=lambda *a: pytest.fail("нельзя"))
    assert not ok and "без one-click" in res


def test_partial_failure_still_ok():
    def bad_post(url):
        raise RuntimeError("HTTP 500")
    ok, res = unsub.unsubscribe(row("<mailto:u@x.ru>, <https://x.ru/u>", "List-Unsubscribe=One-Click"), S,
                                post=bad_post, mail=lambda *a: "письмо на u@x.ru")
    assert ok and "HTTP 500" in res


def test_unknown_mailbox_no_password():
    ok, res = unsub.unsubscribe(row("<mailto:u@x.ru>", mailbox="other@x.ru"), S, mail=lambda *a: pytest.fail())
    assert not ok and "нет пароля" in res


def test_private_hosts_blocked():
    with pytest.raises(ValueError):
        unsub.post_one_click("https://127.0.0.1/u")
    with pytest.raises(ValueError):
        unsub.post_one_click("https://localhost/u")
