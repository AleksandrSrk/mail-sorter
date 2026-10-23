import json

from app import snippet

PLAIN = ("From: a@b.ru\r\nSubject: =?utf-8?b?0KLQtdGB0YI=?=\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
         "Добрый день!\r\n> старая цитата\r\nПредлагаем вагоны https://x.ru/track?id=1 по выгодной цене.\r\n"
         "С уважением,\r\nИван").encode()
HTML = ("From: a@b.ru\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        "<html><style>p{}</style><body><p>Акция &laquo;Осень&raquo;</p><br>Только сегодня</body></html>").encode()


def test_plain_and_html():
    t = snippet.body_text(PLAIN)
    assert snippet.short(t) == "Добрый день! Предлагаем вагоны по выгодной цене."
    assert snippet.short(snippet.body_text(HTML)) == "Акция «Осень» Только сегодня"


def test_short_limit():
    s = snippet.short("слово " * 200, limit=50)
    assert len(s) <= 51 and s.endswith("…")


def test_summarize_without_key_and_with_llm(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert snippet.summarize("Тест", "Добрый день! Предлагаем вагоны.") == "Добрый день! Предлагаем вагоны."
    monkeypatch.setenv("LLM_API_KEY", "k")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Компания предлагает аренду вагонов."}}]}).encode()

    def fake_urlopen(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(snippet.urllib.request, "urlopen", fake_urlopen)
    assert snippet.summarize("Тест", "текст") == "Компания предлагает аренду вагонов."
    assert seen["auth"] == "Bearer k"

    def broken(req, timeout):
        raise OSError("нет сети")

    monkeypatch.setattr(snippet.urllib.request, "urlopen", broken)
    assert snippet.summarize("Тест", "Запасной текст.") == "Запасной текст."


def test_missing_or_wrong_charset():
    utf = "From: a\r\n\r\nПривет, это без charset".encode()
    assert snippet.body_text(utf) == "Привет, это без charset"
    win = "From: a\r\nContent-Type: text/plain\r\n\r\nПисьмо в 1251".encode("cp1251")
    assert snippet.body_text(win) == "Письмо в 1251"


def test_summarize_many(monkeypatch):
    letters = [{"date": "2026-10-23", "subject": "Скидки", "text": "Купите вагоны."},
               {"date": "2026-10-20", "subject": "Напоминание", "text": "Ждём ответа по вагонам."}]
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert snippet.summarize_many(letters) == "Купите вагоны. (всего писем для сводки: 2)"
    assert snippet.summarize_many([]) == ""
    monkeypatch.setenv("LLM_API_KEY", "k")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Продавец вагонов настойчиво предлагает купить."}}]}).encode()

    def fake_urlopen(req, timeout):
        seen["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(snippet.urllib.request, "urlopen", fake_urlopen)
    many = letters * 4
    assert snippet.summarize_many(many) == "Продавец вагонов настойчиво предлагает купить."
    user = seen["body"]["messages"][1]["content"]
    assert "Письмо 5" in user and "Письмо 6" not in user          # не больше пяти писем
    assert "несколько писем" in seen["body"]["messages"][0]["content"]


def test_reply_quotes_cut():
    t = ("Добрый день, счёт оплатим в пятницу.\n\nОт: Иван <ivan@corp.ru>\nОтправлено: 20.10.2026\nСтарая переписка")
    assert snippet.short(t) == "Добрый день, счёт оплатим в пятницу."
    t = "Ок, принято.\n-----Original Message-----\nFrom: a@b.ru\nстарое"
    assert snippet.short(t) == "Ок, принято."
    t = "Согласен.\n20.10.2026, 12:00, Иван пишет:\nстарое"
    assert snippet.short(t) == "Согласен."


def test_fit_letters_shrinks_then_drops_oldest():
    boxes = {"a@our.ru": [{"dir": "in", "date": f"2026-10-{d:02d}", "subject": "т", "text": "с" * 1500}
                          for d in range(1, 21)]}
    items, dropped = snippet.fit_letters(boxes, budget=3000)
    assert all(len(t) <= 201 for _, _, t in items)                     # укоротили до минимума
    assert dropped == 20 - len(items) and dropped > 0
    assert items[0][1]["date"] == "2026-10-20"                         # выкинули самые старые
    items, dropped = snippet.fit_letters(boxes, budget=10 ** 6)
    assert dropped == 0 and len(items) == 20 and len(items[0][2]) <= 601


def test_summarize_correspondence(monkeypatch):
    boxes = {"b@our.ru": [{"dir": "out", "date": "2026-10-21", "subject": "Re: ставка", "text": "Согласны на 1500."}],
             "a@our.ru": [{"dir": "in", "date": "2026-10-20", "subject": "ставка", "text": "Предлагаем 1500 в сутки."}]}
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert snippet.summarize_correspondence("x@corp.ru", boxes) == (None, "не задан LLM_API_KEY", 2, 0)
    assert snippet.summarize_correspondence("x@corp.ru", {})[1] == "текстов писем нет"
    monkeypatch.setenv("LLM_API_KEY", "k")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Договорились о ставке."}}]}).encode()

    def fake_urlopen(req, timeout):
        seen["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(snippet.urllib.request, "urlopen", fake_urlopen)
    assert snippet.summarize_correspondence("x@corp.ru", boxes) == ("Договорились о ставке.", None, 2, 0)
    user = seen["body"]["messages"][1]["content"]
    assert user.index("=== Ящик a@our.ru ===") < user.index("=== Ящик b@our.ru ===")
    assert "[от него, 2026-10-20] Тема: ставка" in user and "[от нас, 2026-10-21] Тема: Re: ставка" in user

    def limited(req, timeout):
        raise snippet.urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)

    monkeypatch.setattr(snippet.urllib.request, "urlopen", limited)
    text, err, _, _ = snippet.summarize_correspondence("x@corp.ru", boxes)
    assert text is None and "лимит Groq (429)" in err
