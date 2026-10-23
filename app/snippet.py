"""Краткое содержание письма для уведомлений: тема + начало текста без цитат и подписей.
Опционально — пересказ LLM через OpenAI-совместимый API (Groq, xAI и т.п.), если задан LLM_API_KEY."""
import email
import email.policy
import html
import json
import logging
import os
import re
import urllib.request

log = logging.getLogger("snippet")
SIGNATURE = re.compile(r"^(--\s*$|с уважением|best regards|kind regards|regards,|отправлено с|sent from)", re.I)


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
        if SIGNATURE.match(line):
            break
        if line:
            lines.append(line)
    out = re.sub(r"\s+", " ", " ".join(lines)).strip()
    out = re.sub(r"<https?://[^>]+>|https?://\S+", "", out)        # ссылки в уведомлении не нужны
    out = re.sub(r"\s+", " ", out).strip()
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


def _llm(system: str, user: str):
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    url = os.environ.get("LLM_API_URL", "https://api.x.ai/v1/chat/completions")
    model = os.environ.get("LLM_MODEL", "grok-3-mini")
    body = {"model": model, "temperature": 0.2, "max_tokens": 300,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()[:800] or None
    except Exception as e:
        log.warning("LLM-пересказ не удался (%s), беру начало текста", type(e).__name__)
        return None


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
