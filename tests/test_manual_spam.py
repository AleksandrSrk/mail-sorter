"""Кнопка «В СПАМ»: адрес или домен вручную, поддомены, защиты, отчёты."""
from collections import Counter

from app import views
from app.config import Rule, domain_chain, domain_in
from app.main import format_report
from tests.test_sorter import FakeIMAP, make


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def desk_for(store, clock=None):
    return views.SpamDesk(store, own_domains={"our.ru"}, clock=clock or Clock())


def entry(store, kind, value):
    return store.one("SELECT * FROM entries WHERE rule_id=? AND kind=? AND value=?",
                     store.learn_rule()["id"], kind, value)


def test_domain_chain_and_subdomain_rules():
    assert domain_chain("A.B.example.ru.") == ["a.b.example.ru", "b.example.ru", "example.ru"]
    assert domain_chain("example.ru") == ["example.ru"]
    assert domain_in("mail.spam.ru", {"spam.ru"}) and not domain_in("notspam.ru", {"spam.ru"})
    rule = Rule(name="s", move_to="Junk", sender_domains={"spam.ru"}, exempt_domains={"good.spam.ru"})
    assert rule.matches("x@spam.ru", "") and rule.matches("x@a.b.spam.ru", "")
    assert not rule.matches("x@notspam.ru", "")
    assert not rule.matches("x@good.spam.ru", "") and not rule.matches("x@deep.good.spam.ru", "")


def test_parse_targets():
    assert views.parse_targets("steve@spamlog.example") == [("addr", "steve@spamlog.example")]
    assert views.parse_targets("Yara <Steve@Spamlog.example>") == [("addr", "steve@spamlog.example")]
    assert views.parse_targets("@junk.ru") == [("domain", "junk.ru")]
    assert views.parse_targets("https://www.Junk.ru/path?x=1, mail.other.com.") == \
        [("domain", "junk.ru"), ("domain", "mail.other.com")]
    assert views.parse_targets("a@b.ru junk.ru a@b.ru") == [("addr", "a@b.ru"), ("domain", "junk.ru")]
    assert views.parse_targets("просто текст, e-mail: нет") == []
    assert views.parse_targets("версия 1.5") == []


def test_manual_spam_address_and_domain(store):
    assert store.manual_spam("addr", "Steve@Spamlog.example") == "added"
    e = entry(store, "addr", "steve@spamlog.example")
    assert e["source"] == "manual" and e["status"] == "active" and e["no_unsub"] == 1
    assert store.manual_spam("addr", "steve@spamlog.example") is None            # уже в фильтре
    assert store.manual_spam("domain", "@Spam-Corp.ru") == "added"
    assert store.manual_spam("addr", "x@mail.spam-corp.ru") is None             # покрыт доменом, в т.ч. поддоменом
    assert store.manual_spam("domain", "mail.spam-corp.ru") is None
    assert not store.pending_unsubs()
    rule = [r for r in store.rules_for("box@our.ru") if r.id == store.learn_rule()["id"]][0]
    assert rule.matches("any@sub.spam-corp.ru", "") and rule.matches("steve@spamlog.example", "")
    reports = {r["sender"]: r for r in store.q("SELECT * FROM reports")}
    assert set(reports) == {"steve@spamlog.example", "@spam-corp.ru"} and reports["@spam-corp.ru"]["mailbox"] == ""


def test_manual_spam_returns_unsubscribed_without_unsub(store):
    store.learn("n@x.ru", "box@our.ru", "<mailto:u@x.ru>")
    store.finish_unsub(store.pending_unsubs()[0]["id"], True, "ок")
    store.x("UPDATE entries SET status='unsubscribed' WHERE value='n@x.ru'")
    assert store.manual_spam("addr", "n@x.ru") == "returned"
    e = entry(store, "addr", "n@x.ru")
    assert e["status"] == "active" and e["no_unsub"] == 1


def test_manual_spam_takes_address_out_of_bank_rule(store):
    bank = store.add_rule("*", "БАНК", "БАНК", learn=True, position=50)
    store.add_entry(bank, "addr", "robot@bank.ru", "seed")
    assert store.manual_spam("addr", "robot@bank.ru") == "added"
    assert store.one("SELECT status FROM entries WHERE rule_id=? AND value='robot@bank.ru'", bank)["status"] == "removed"


def test_desk_confirm_flow_and_cancel(store):
    desk = desk_for(store)
    text, rows = desk.request("steve@spamlog.example, @junk2.ru")
    assert "Добавлю в спам-фильтр (2)" in text and "домен junk2.ru (и все поддомены)" in text
    assert [t for t, _ in rows[0]] == ["✅ ОК, в спам", "Отмена"]
    assert entry(store, "addr", "steve@spamlog.example") is None                # до ОК ничего не записано
    result, note = desk.decide(rows[0][0][1])
    assert note == "Принято" and "В спам-фильтре (2)" in result
    assert entry(store, "addr", "steve@spamlog.example") and entry(store, "domain", "junk2.ru")
    assert desk.decide(rows[0][0][1]) == (None, "Устаревшая кнопка")           # второй раз — устарело

    text, rows = desk.request("other@spam-me.ru")
    result, note = desk.decide(rows[0][1][1])
    assert note == "Отменено" and entry(store, "addr", "other@spam-me.ru") is None
    assert all(len(d.encode()) <= 64 for row in rows for _, d in row)


def test_desk_duplicates_and_own_domains(store):
    desk = desk_for(store)
    desk.decide(desk.request("steve@spamlog.example junk2.ru")[1][0][0][1])
    text, rows = desk.request("steve@spamlog.example x@sub.junk2.ru junk2.ru boss@our.ru mail.our.ru")
    assert rows == [] and "Добавлять нечего" in text
    assert "уже в спам-фильтре" in text and "уже покрыт: домен junk2.ru" in text
    assert "Свои домены" in text and "boss@our.ru" in text and "mail.our.ru" in text
    # смесь: часть новая — кнопки есть, дубли и свои домены просто перечислены
    text, rows = desk.request("steve@spamlog.example fresh@new.ru")
    assert "Добавлю в спам-фильтр (1)" in text and "Уже в спаме" in text and rows


def test_desk_headline_shows_check_result(store):
    desk = desk_for(store)
    first = lambda t: t.splitlines()[0]  # noqa: E731
    text, rows = desk.request("fresh@new.ru")
    assert first(text) == "🚫 В СПАМ — fresh@new.ru: новый, в фильтре его ещё нет"
    desk.decide(rows[0][0][1])
    assert first(desk.request("fresh@new.ru")[0]) == "🚫 В СПАМ — fresh@new.ru: уже в спам-фильтре"
    assert first(desk.request("x@sub.new.ru")[0]) == "🚫 В СПАМ — x@sub.new.ru: новый, в фильтре его ещё нет"
    desk.decide(desk.request("junk3.ru")[1][0][0][1])
    assert first(desk.request("a@junk3.ru")[0]) == \
        "🚫 В СПАМ — a@junk3.ru: уже покрыт: домен junk3.ru в спам-фильтре"
    assert first(desk.request("gmail.com")[0]) == "🚫 В СПАМ — домен gmail.com: под защитой, не добавил"
    assert first(desk.request("boss@our.ru")[0]) == "🚫 В СПАМ — boss@our.ru: свой домен, не трогаю"
    assert first(desk.request("fresh@new.ru junk3.ru")[0]) == "🚫 В СПАМ — всё уже в спаме (2)"
    mixed = desk.request("fresh@new.ru new2@new.ru yahoo.com boss@our.ru")[0]   # gmail.com уже «предупреждён» выше
    assert first(mixed) == "🚫 В СПАМ — новых: 1, уже в спаме: 1, под защитой: 1, свои домены: 1"
    assert first(desk.request("n1@a.ru n2@b.ru")[0]) == "🚫 В СПАМ — новых: 2"


def test_desk_whitelist_needs_resend_within_five_minutes(store):
    clock = Clock()
    desk = desk_for(store, clock)
    text, rows = desk.request("gmail.com")
    assert rows == [] and "Защита от случайности" in text and "бесплатная почта" in text
    assert entry(store, "domain", "gmail.com") is None
    clock.t += 120
    text, rows = desk.request("gmail.com")                          # повтор — подтверждение
    assert rows and "подтверждено повторной отправкой" in text
    desk.decide(rows[0][0][1])
    assert entry(store, "domain", "gmail.com")
    # просрочка: предупреждение действует 5 минут
    clock.t += 10
    text, rows = desk.request("yandex.ru")
    assert rows == []
    clock.t += 301
    text, rows = desk.request("yandex.ru")
    assert rows == [] and "Защита от случайности" in text
    # а конкретный адрес на бесплатной почте — без предупреждений
    text, rows = desk.request("spammer123@yahoo.com")
    assert rows and "Защита" not in text


def test_desk_protects_addresses_from_other_rules(store):
    desk = desk_for(store)
    text, rows = desk.request("p@mail.ru")                          # есть в правиле «Партнёр»
    assert rows == [] and "есть в правиле «Партнёр»" in text
    text, rows = desk.request("p@mail.ru")
    assert rows and "подтверждено повторной отправкой" in text
    bank = store.add_rule("*", "БАНК", "БАНК", learn=True, position=50)
    store.add_entry(bank, "domain", "mybank.ru", "seed")
    text, rows = desk.request("alert@mail.mybank.ru")               # домен банка покрывает и поддомен
    assert rows == [] and "«БАНК»" in text


def test_desk_notes_contacts_and_exempt_history(store):
    store.add_contacts(["client@corp.ru", "x@corp.ru"])
    desk = desk_for(store)
    text, rows = desk.request("client@corp.ru corp.ru")
    assert "мы туда писали 1 раз" in text and "мы туда писали 2 раз" in text and rows
    store.add_entry(store.learn_rule()["id"], "addr", "ex@zz.ru", "bot", status="removed")
    text, _ = desk.request("ex@zz.ru")
    assert "раньше снимал" in text
    assert desk.request("пусто")[1] == []


def test_domain_report_counts_subdomain_letters(store):
    rid = store.learn_rule()["id"]
    store.manual_spam("domain", "dom.ru")
    store.manual_spam("addr", "one@alone.ru")
    moved = Counter({"a@dom.ru": 2, "b@sub.dom.ru": 1, "c@other.ru": 5, "one@alone.ru": 4})
    swept = {f"{rid}:d:dom.ru", f"{rid}:a:one@alone.ru"}
    store.report_progress("m1@our.ru", swept, moved)
    store.report_progress("m2@our.ru", swept, Counter())
    ready = {r["sender"]: r for r in store.ready_reports(["m1@our.ru", "m2@our.ru"])}
    d = ready["@dom.ru"]
    assert d["boxes"] == {"m1@our.ru": 3, "m2@our.ru": 0} and d["domain"] and d["manual"]
    text = format_report(d)
    assert "добавлен: домен @dom.ru (и все поддомены)" in text and "Добавлено вручную из бота" in text
    assert "перенесены в спам: 3" in text and "• m1@our.ru — 3" in text
    assert "В остальных 1 ящиках писем от этого домена не было." in text and "Отписку не делаю" in text
    assert "Кинули в спам в ящике" not in text and "заголовка для отписки" not in text
    a = ready["one@alone.ru"]
    assert a["boxes"]["m1@our.ru"] == 4 and a["manual"] and not a["domain"]
    assert "от него не было" in format_report(a)


def test_sweep_finds_subdomain_senders_and_moves_them(store):
    fake = FakeIMAP({
        "INBOX": {"validity": 1, "msgs": {1: ("x@spamlog.example", "a", False), 2: ("y@mail.spamlog.example", "b", False),
                                           3: ("z@notspamlog.example.ru", "c", False), 4: ("ok@fine.ru", "d", False)}},
        "Junk": {"validity": 1, "msgs": {}},
    })
    s = make(fake, store)
    assert s.run_once(full=True)[0] == 0
    store.manual_spam("domain", "spamlog.example")
    moved, _ = s.run_once()
    assert moved == 2 and sorted(fake.f["INBOX"]["msgs"]) == [3, 4]             # домен и его поддомен; чужой — нет
    assert {n for _, n in fake.searches} == {"spamlog.example"}
    rid = store.learn_rule()["id"]
    r = store.one("SELECT boxes FROM reports WHERE sender='@spamlog.example'")
    assert r and '"me@our.ru": 2' in r["boxes"]
    assert f"{rid}:d:spamlog.example" in s.state["_swept"]


def test_restore_domain_returns_subdomain_letters(store):
    fake = FakeIMAP({
        "INBOX": {"validity": 1, "msgs": {}},
        "Junk": {"validity": 1, "msgs": {1: ("x@spamlog.example", "a", False), 2: ("y@mail.spamlog.example", "b", False),
                                          3: ("z@other.ru", "c", False)}},
    })
    s = make(fake, store)
    store.manual_spam("domain", "spamlog.example")
    eid = entry(store, "domain", "spamlog.example")["id"]
    store.queue_restore(eid)
    s.run_once()
    assert sorted(fake.f["INBOX"]["msgs"]) == [1, 2] and sorted(fake.f["Junk"]["msgs"]) == [3]
