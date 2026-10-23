"""Краткое содержание письма для уведомлений: тема + начало текста без цитат и подписей.
Опционально — пересказ LLM через OpenAI-совместимый API (Groq, xAI и т.п.), если задан LLM_API_KEY."""
import email
import email.policy
import html
import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger("snippet")
SIGNATURE = re.compile(r"^(--\s*$|с уважением|best regards|kind regards|regards,|отправлено с|sent from)", re.I)
# начало процитированного письма в ответах (Outlook, веб-почта, телефоны) — дальше старая переписка
QUOTE_START = re.compile(r"^(-{2,}\s*(original message|исходное сообщение|пересылаемое сообщение|forwarded message)"
                         r"|(от|from):\s.*@|.*(писал|пишет|wrote)\s*:\s*$)", re.I)


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def _decode_part(part) -> str:
    """Кодировка из заголовка; если её нет или она врёт — UTF-8, потом Windows-1251."""
    payload = part.get_payload(decode=True) or b""
    for enc in filter(None, [part.get_content_charset(), "utf-8", "cp1251"]):
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def body_text(raw: bytes) -> str:
    """Текст письма из сырого (возможно, обрезанного) MIME: text/plain, иначе html без тегов."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    plain, htm = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment" or ctype not in ("text/plain", "text/html"):
            continue
        content = _decode_part(part)
        if ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "text/html" and htm is None:
            htm = content
    return plain if plain and plain.strip() else _html_to_text(htm or "")


def short(text: str, limit=300) -> str:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(">"):
            continue
        if SIGNATURE.match(line) or QUOTE_START.match(line):
            break
        if line:
            lines.append(line)
    out = re.sub(r"\s+", " ", " ".join(lines)).strip()
    out = re.sub(r"<https?://[^>]+>|https?://\S+", "", out)        # ссылки в уведомлении не нужны
    out = re.sub(r"\s+", " ", out).strip()
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


LLM_ERRORS = {401: "ключ Groq не принят (401)", 413: "запрос слишком большой для лимита (413)",
              429: "упёрлись в лимит Groq (429) — попробуй через минуту"}


def _llm_call(system: str, user: str, max_tokens=300, limit=800):
    """-> (текст, None) или (None, причина по-человечески)."""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None, "не задан LLM_API_KEY"
    url = os.environ.get("LLM_API_URL", "https://api.x.ai/v1/chat/completions")
    model = os.environ.get("LLM_MODEL", "grok-3-mini")
    body = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "gpt-oss" in model:      # рассуждающая модель: «мысли» едят тот же лимит токенов, что и ответ
        body["reasoning_effort"] = "low"
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                              "User-Agent": "mail-sorter/1.0"})  # Cloudflare режет Python-urllib
        with urllib.request.urlopen(req, timeout=60) as r:
            choice = json.load(r)["choices"][0]
        text = (choice["message"].get("content") or "").strip()[:limit]
        if not text:
            return None, "пустой ответ модели"
        if choice.get("finish_reason") == "length":
            text += "\n\n(ответ обрезан — не хватило лимита токенов)"
        return text, None
    except urllib.error.HTTPError as e:
        why = LLM_ERRORS.get(e.code, f"ошибка API {e.code}")
    except Exception as e:
        why = f"нет ответа ({type(e).__name__})"
    log.warning("LLM не ответила: %s", why)
    return None, why


def _llm(system: str, user: str):
    return _llm_call(system, user)[0]


def summarize(subject: str, text: str) -> str:
    """Краткое описание одного письма. Без LLM_API_KEY или при ошибке — начало текста."""
    if not text.strip():
        return ""
    return _llm("Перескажи письмо по-русски в 1–2 предложениях: кто пишет и что предлагает или просит. "
                "Без вступлений и оценок.", f"Тема: {subject}\n\n{text[:4000]}") or short(text)


def summarize_many(letters, limit=5) -> str:
    """Общая сводка по последним письмам отправителя. letters: [{"date", "subject", "text"}], свежие первыми."""
    letters = [x for x in letters if (x.get("text") or "").strip()][:limit]
    if not letters:
        return ""
    if len(letters) == 1:
        return summarize(letters[0]["subject"], letters[0]["text"])
    user = "\n\n".join(f"Письмо {i} ({x.get('date', '')[:10]}), тема: {x['subject']}\n{x['text'][:1500]}"
                        for i, x in enumerate(letters, 1))
    got = _llm("Ниже несколько писем от одного отправителя. Сделай по-русски общую сводку в 2–3 предложениях: "
               "кто пишет, о чём эти письма и чего от нас хотят. Без вступлений и оценок.", user)
    return got or f"{short(letters[0]['text'])} (всего писем для сводки: {len(letters)})"


CORRESPONDENCE_PROMPT = (
    "Ты помогаешь директору транспортной компании понять, кто такой внешний адрес и что у нас с ним. "
    "Ниже письма по нашим почтовым ящикам. Каждое письмо подписано направлением: "
    "«ОН → НАМ» — его письмо нам, «МЫ → ЕМУ» — наше письмо ему. Просьбы в письмах «МЫ → ЕМУ» — это НАШИ просьбы к нему, "
    "а не его к нам. Ответ по-русски, простым текстом без markdown и звёздочек, строго по форме:\n"
    "Кто это: одно предложение.\n"
    "По ящикам: для каждого ящика одна строка «ящик — что писал он, что писали мы, на чём остановились».\n"
    "Что он хочет от нас: только по письмам «ОН → НАМ»; если только реклама — «ничего, только рассылка».\n"
    "Что мы хотели от него: только по письмам «МЫ → ЕМУ»; если таких нет — «мы ему не писали».\n"
    "Вывод: живая переписка, рассылка или похоже на спам — и коротко почему.\n"
    "Опирайся только на текст писем, не выдумывай.")


def fit_letters(boxes: dict, budget=12000, per_letter=600, floor=200):
    """Укладываем письма в бюджет символов (≈ лимит токенов Groq): сначала укорачиваем каждое письмо
    до floor, потом выкидываем самые старые. -> (список (ящик, письмо, текст), выкинуто писем)."""
    items = [(mb, x) for mb, xs in boxes.items() for x in xs if (x.get("text") or "").strip()]
    items.sort(key=lambda it: it[1].get("date", ""), reverse=True)
    limit = per_letter
    while limit > floor and sum(min(len(x["text"]), limit) + len(x.get("subject", "")) + 40 for _, x in items) > budget:
        limit -= 100
    dropped = 0
    while items and sum(min(len(x["text"]), limit) + len(x.get("subject", "")) + 40 for _, x in items) > budget:
        items.pop()
        dropped += 1
    return [(mb, x, short(x["text"], limit)) for mb, x, in items], dropped


def summarize_correspondence(address: str, boxes: dict, budget=10000):
    """boxes: {ящик: [{"dir": "in"|"out", "date", "subject", "text"}]} -> (текст | None, ошибка | None, писем, выкинуто)."""
    items, dropped = fit_letters(boxes, budget)
    if not items:
        return None, "текстов писем нет", 0, 0
    parts = []
    for mb in sorted({mb for mb, _, _ in items}):
        parts.append(f"=== Ящик {mb} ===")
        for _, x, text in sorted((it for it in items if it[0] == mb), key=lambda it: it[1].get("date", "")):
            who = "ОН → НАМ" if x.get("dir") == "in" else "МЫ → ЕМУ"
            parts.append(f"[{who}, {x.get('date', '')[:10]}] Тема: {x.get('subject', '')}\n{text}")
    text, err = _llm_call(CORRESPONDENCE_PROMPT, f"Адрес: {address}\n\n" + "\n\n".join(parts),
                          max_tokens=2000, limit=3500)
    return text, err, len(items), dropped
