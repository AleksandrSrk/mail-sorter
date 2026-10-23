"""Списки и действия для Telegram-бота. Без зависимостей от Telegram — чтобы тестировать.

Экран = (текст, ряды кнопок [[(подпись, callback_data), ...], ...]).
callback_data: "<список>:<действие>:<id>:<страница>"  (лимит Telegram — 64 байта)."""
import re
from math import ceil

from .db import IN_FILTER, Store, domain_of, now_iso
from .scoring import FREE, SUSPICIOUS, score_addr, score_domain
from .snippet import summarize_correspondence

PAGE = 10
STATUS = {
    "active": "",
    "unsubscribing": "📭 отписались, ждём тишину",
    "unsubscribed": "🔕 отписаны, сняты с фильтра",
    "removed": "↩️ снят с фильтра",
}


def _learn_id(store: Store):
    r = store.learn_rule()
    return r["id"] if r else None


def _page(items, page):
    pages = max(1, ceil(len(items) / PAGE))
    page = min(max(page, 0), pages - 1)
    return items[page * PAGE:(page + 1) * PAGE], page, pages


def _nav(prefix, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(("◀️", f"{prefix}:p:0:{page - 1}"))
    row.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(("▶️", f"{prefix}:p:0:{page + 1}"))
    return [row]


def _sorted(items):
    """Подозрительно полезные — наверх, дальше по алфавиту."""
    return sorted(items, key=lambda it: (-(it["score"] >= SUSPICIOUS), -it["score"], it["value"]))


def _mark(it):
    parts = []
    reasons = ", ".join(it["why"])
    if it["score"] >= SUSPICIOUS:
        parts.append("⚠️ похоже, нужный: " + reasons)
    elif reasons:
        parts.append(reasons)
    st = STATUS.get(it.get("status", ""), "")
    if st:
        parts.append(st)
    return ("\n     " + " · ".join(parts)) if parts else ""


# ---------- данные ----------
def _addr_items(store, hidden):
    rid = _learn_id(store)
    if rid is None:
        return []
    by_addr, by_domain = store.contacts()
    rows = store.q(f"SELECT id, value, status FROM entries WHERE rule_id=? AND kind='addr' AND hidden=? "
                   f"AND status IN ({','.join('?' * len(IN_FILTER))})", rid, int(hidden), *IN_FILTER)
    out = []
    for r in rows:
        s, why = score_addr(r["value"], by_addr, by_domain)
        out.append({"id": r["id"], "value": r["value"], "status": r["status"], "score": s, "why": why})
    return _sorted(out)


def _domain_items(store, hidden):
    rid = _learn_id(store)
    if rid is None:
        return []
    _, by_domain = store.contacts()
    counts, whole = {}, set()
    for r in store.q(f"SELECT kind, value FROM entries WHERE rule_id=? AND kind IN ('addr','domain') "
                     f"AND status IN ({','.join('?' * len(IN_FILTER))})", rid, *IN_FILTER):
        if r["kind"] == "domain":
            whole.add(r["value"])
            counts.setdefault(r["value"], 0)
        else:
            d = r["value"].rsplit("@", 1)[1]
            counts[d] = counts.get(d, 0) + 1
    out = []
    for d, n in counts.items():
        store.x("INSERT OR IGNORE INTO domains(domain) VALUES(?)", d)
        row = store.one("SELECT id, hidden FROM domains WHERE domain=?", d)
        if bool(row["hidden"]) != hidden:
            continue
        s, why = score_domain(d, by_domain)
        out.append({"id": row["id"], "value": d, "n": n, "whole": d in whole, "score": s, "why": why})
    return _sorted(out)


# ---------- экраны ----------
def addresses(store, page=0):
    items, page, pages = _page(_addr_items(store, hidden=False), page)
    total = len(_addr_items(store, hidden=False))
    if not total:
        return "📧 Спам-адресов нет.", []
    lines = [f"📧 Спам-адреса: {total}  (✅ — не спам, убрать из фильтра; 🙈 — скрыть из списка)\n"]
    rows = []
    for i, it in enumerate(items, page * PAGE + 1):
        lines.append(f"{i}. {it['value']}{_mark(it)}")
        rows.append([(f"{i} ✅ не спам", f"a:del:{it['id']}:{page}"), (f"{i} 🙈 скрыть", f"a:hide:{it['id']}:{page}")])
    return "\n".join(lines), rows + _nav("a", page, pages)


def domains(store, page=0):
    all_items = _domain_items(store, hidden=False)
    items, page, pages = _page(all_items, page)
    if not all_items:
        return "🌐 Спам-доменов нет.", []
    lines = [f"🌐 Спам-домены: {len(all_items)}  (✅ — не спам: убрать домен и все его адреса из фильтра, "
             f"🙈 — скрыть, ➕ — блокировать весь домен)\n"]
    rows = []
    for i, it in enumerate(items, page * PAGE + 1):
        what = "весь домен в фильтре" if it["whole"] else f"адресов в фильтре: {it['n']}"
        lines.append(f"{i}. {it['value']} — {what}{_mark(it)}")
        row = [(f"{i} ✅ не спам", f"d:del:{it['id']}:{page}"), (f"{i} 🙈", f"d:hide:{it['id']}:{page}")]
        if not it["whole"] and it["value"] not in FREE:
            row.append((f"{i} ➕ весь домен", f"d:all:{it['id']}:{page}"))
        rows.append(row)
    return "\n".join(lines), rows + _nav("d", page, pages)


def hidden(store, page=0):
    items = [dict(it, t="a") for it in _addr_items(store, hidden=True)] + \
            [dict(it, t="d") for it in _domain_items(store, hidden=True)]
    part, page, pages = _page(items, page)
    if not items:
        return "🙈 Скрытых нет.", []
    lines = [f"🙈 Скрытые: {len(items)}  (✅ — не спам, убрать из фильтра)\n"]
    rows = []
    for i, it in enumerate(part, page * PAGE + 1):
        label = it["value"] if it["t"] == "a" else f"домен {it['value']}"
        lines.append(f"{i}. {label}{_mark(it)}")
        rows.append([(f"{i} ✅ не спам", f"h:del{it['t']}:{it['id']}:{page}")])
    return "\n".join(lines), rows + _nav("h", page, pages)


def unsubscribed(store, page=0):
    rows = store.q("""
        SELECT e.value, e.status, e.no_unsub, e.unsub_at, e.last_seen_at,
               (SELECT result FROM unsubs u WHERE u.entry_id=e.id ORDER BY u.id DESC LIMIT 1) AS result,
               (SELECT mailbox FROM unsubs u WHERE u.entry_id=e.id ORDER BY u.id DESC LIMIT 1) AS mailbox
        FROM entries e WHERE e.unsub_at IS NOT NULL OR e.status='unsubscribed'
        ORDER BY e.unsub_at DESC""")
    if not rows:
        return "📭 Отписок пока не было.", []
    part, page, pages = _page(list(rows), page)
    lines = [f"📭 Отписки: {len(rows)}\n"]
    for i, r in enumerate(part, page * PAGE + 1):
        st = STATUS.get(r["status"], r["status"]) or "в фильтре"
        if r["no_unsub"]:
            st += " · больше не отписываемся"
        lines.append(f"{i}. {r['value']} — {st}\n     {(r['unsub_at'] or '')[:10]} · {r['mailbox'] or ''} · "
                     f"{r['result'] or ''}"
                     + (f"\n     последнее письмо: {r['last_seen_at'][:16].replace('T', ' ')} UTC"
                        if r["last_seen_at"] else ""))
    return "\n".join(lines), _nav("u", page, pages)


SCREENS = {"a": addresses, "d": domains, "h": hidden, "u": unsubscribed}


# ---------- действия ----------
def _remove_domain(store, rid, domain):
    ts = now_iso()
    store.x(f"UPDATE entries SET status='removed', updated_at=? WHERE rule_id=? AND status IN "
            f"({','.join('?' * len(IN_FILTER))}) AND ((kind='domain' AND value=?) OR (kind='addr' AND value LIKE ?))",
            ts, rid, *IN_FILTER, domain, "%@" + domain)


def act(store, data: str):
    """Выполнить действие по callback_data. Возвращает (экран-обновление, короткий ответ)."""
    try:
        screen, action, ident, page = data.split(":")
        ident, page = int(ident), int(page)
    except ValueError:
        return None, ""
    rid = _learn_id(store)
    note = ""
    ts = now_iso()
    if action == "p":
        pass
    elif screen == "a" and action == "del" or screen == "h" and action == "dela":
        store.x("UPDATE entries SET status='removed', hidden=0, updated_at=? WHERE id=?", ts, ident)
        note = "Убран из спам-фильтра"
    elif screen == "a" and action == "hide":
        store.x("UPDATE entries SET hidden=1, updated_at=? WHERE id=?", ts, ident)
        note = "Скрыт"
    elif screen in ("d", "h") and action in ("del", "deld"):
        d = store.one("SELECT domain FROM domains WHERE id=?", ident)
        if d and rid:
            _remove_domain(store, rid, d["domain"])
            store.x("UPDATE domains SET hidden=0 WHERE id=?", ident)
            note = f"Домен {d['domain']} убран из фильтра"
    elif screen == "d" and action == "hide":
        store.x("UPDATE domains SET hidden=1 WHERE id=?", ident)
        note = "Скрыт"
    elif screen == "d" and action == "all":
        d = store.one("SELECT domain FROM domains WHERE id=?", ident)
        if d and d["domain"] in FREE:
            return SCREENS[screen](store, page), "Бесплатную почту целиком блокировать нельзя"
        if d and rid:
            eid = store.add_entry(rid, "domain", d["domain"], "bot")
            store.x("UPDATE entries SET status='active', updated_at=? WHERE id=?", ts, eid)
            note = f"Весь домен {d['domain']} в спам-фильтре"
    else:
        return None, ""
    return SCREENS[screen](store, page), note


# ---------- поиск и ручное снятие адреса ----------
ADDR_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
STATUS_FULL = {
    "active": "🔴 в фильтре",
    "unsubscribing": "📭 в фильтре, отписались — ждём неделю тишины",
    "unsubscribed": "🔕 отписаны, сняты с фильтра",
    "removed": "⚪ снят с фильтра",
}


def parse_addresses(text: str) -> list[str]:
    seen = []
    for a in ADDR_RE.findall(text or ""):
        a = a.lower().strip(".")
        if a not in seen:
            seen.append(a)
    return seen


def check_address(store, addr: str, own_domains=()) -> str:
    return check_screen(store, addr, own_domains)[0]


def check_screen(store, addr: str, own_domains=()):
    """Текст проверки + кнопки «убрать из этого правила, письма во входящие» для активных записей."""
    dom = addr.rsplit("@", 1)[1]
    lines = [f"🔎 {addr}"]
    if dom in own_domains:
        lines.append("Свой домен — спамом не считается и автоматически не учится.")
    rows = store.q("SELECT e.*, r.name AS rule, r.mailbox AS rmb, r.unsubscribe AS is_spam FROM entries e "
                   "JOIN rules r ON r.id=e.rule_id WHERE (e.kind='addr' AND e.value=?) OR (e.kind='domain' AND e.value=?) "
                   "ORDER BY r.position, r.id", addr, dom)
    if not rows:
        lines.append("Ни в одном списке нет.")
    for e in rows:
        where = f"«{e['rule']}»" + ("" if e["rmb"] == "*" else f" (только {e['rmb']})")
        what = "адрес" if e["kind"] == "addr" else f"весь домен @{e['value']}"
        st = STATUS_FULL.get(e["status"], e["status"])
        extra = [f"источник: {e['source']}", f"с {(e['created_at'] or '')[:10]}"]
        if e["hidden"]:
            extra.append("скрыт в боте")
        if e["no_unsub"]:
            extra.append("больше не отписываемся")
        if e["last_seen_at"]:
            extra.append(f"последнее письмо {e['last_seen_at'][:10]}")
        lines.append(f"• {where}: {what} — {st}\n   " + ", ".join(extra))
        if e["kind"] == "addr" and e["is_spam"]:
            for u in store.q("SELECT mailbox, status, result FROM unsubs WHERE entry_id=? ORDER BY id", e["id"]):
                mark = {"done": "✅", "failed": "❌"}.get(u["status"], "⏳")
                lines.append(f"   отписка {mark} {u['mailbox']}: {u['result'] or 'в очереди'}")
    sent = store.one("SELECT sent FROM contacts WHERE address=?", addr)
    if sent:
        boxes = store.box_contacts(addr)
        lines.append(f"Мы ему писали: {letters(sent['sent'])}" + ("" if boxes else " (по ящикам — после ближайшего прохода)"))
        lines += [f"  • {_short_box(mb)} — {n}" for mb, n in sorted(boxes.items(), key=lambda x: (-x[1], x[0]))]
    else:
        lines.append("Мы ему не писали.")
    buttons = []
    own_addr_rules = {e["rule_id"] for e in rows if e["kind"] == "addr"}
    for e in rows:
        if e["status"] not in IN_FILTER:
            continue
        if e["kind"] == "addr":
            buttons.append([(f"↩️ Убрать из «{e['rule']}» → во входящие", f"x:rm:{e['id']}:0")])
            continue
        buttons.append([(f"↩️ Убрать весь домен @{e['value']} из «{e['rule']}»", f"x:rm:{e['id']}:0")])
        data = f"x:ex:{e['rule_id']}:{addr}"
        if e["rule_id"] not in own_addr_rules and len(data.encode()) <= 64:
            buttons.append([(f"↩️ Только {addr} — исключение, во входящие", data)])
    spam = store.learn_rule()
    if spam and spam["spam_header"] and dom not in own_domains and \
            not any(e["rule_id"] == spam["id"] for e in rows):
        lines.append("Спам-фильтр может поймать его по серверной метке спама.")
        data = f"x:ex:{spam['id']}:{addr}"
        if len(data.encode()) <= 64:
            buttons.append([("✅ Не спам — не трогать по метке сервера, письма во входящие", data)])
    return "\n".join(lines), buttons


LOOKUP_NOTE = "⏳ Смотрю его письма во всех ящиках и папках — отчёт придёт через минуту-две."


def check_with_lookup(store, addr: str, own_domains=()):
    """Проверка из бота: правила сразу + поиск писем по ящикам в очередь сортировщику."""
    text, rows = check_screen(store, addr, own_domains)
    store.queue_lookup(addr)
    return f"{text}\n\n{LOOKUP_NOTE}", rows


def exempt(store, rule_id: int, addr: str, own_domains=()):
    """Адрес под доменом, заблокированным целиком: исключение только для него + возврат его писем."""
    if not store.one("SELECT 1 FROM rules WHERE id=?", rule_id) or not ADDR_RE.fullmatch(addr):
        return None, "Устаревшая кнопка"
    eid = store.add_entry(rule_id, "addr", addr, "bot")
    return restore(store, eid, own_domains)


def restore(store, entry_id: int, own_domains=()):
    """Кнопка из проверки: снять запись с правила, письма вернёт сортировщик. -> (экран, ответ)"""
    e = store.queue_restore(entry_id)
    if not e:
        return None, "Запись не найдена"
    addr = e["value"] if e["kind"] == "addr" else None
    note = (f"Убран из «{e['rule_name']}». Письма вернутся во входящие в течение пары минут — пришлю сводку.")
    if addr:
        text, rows = check_screen(store, addr, own_domains)
        return (text + "\n\n" + note, rows), "Готово"
    return (f"Домен @{e['value']}: {note}", []), "Готово"


# ---------- серверный спам: уведомление и решение ----------
def _short_box(mb):
    local, _, dom = mb.partition("@")
    return f"{local}@{dom.split('.')[0]}"


def letters(n: int) -> str:
    """2 письма, 5 писем, 21 письмо."""
    if n % 10 == 1 and n % 100 != 11:
        word = "письмо"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "письма"
    else:
        word = "писем"
    return f"{n} {word}"


FOLDER_NAMES = {"INBOX": "Входящие"}


def format_lookup(r) -> str:
    """Отчёт «где его письма»: по ящикам и папкам, сколько мы ему писали, даты."""
    boxes = r["boxes"]
    got = sum(sum(i["folders"].values()) for i in boxes.values())
    sent = sum(i["sent"] for i in boxes.values())
    def by_box(count):
        parts = sorted(((mb, count(i)) for mb, i in boxes.items() if count(i)), key=lambda x: (-x[1], x[0]))
        return ", ".join(f"{_short_box(mb)} {n}" for mb, n in parts)

    lines = [f"📊 {r['address']} — письма по ящикам",
             f"Он нам: {letters(got)} — {by_box(lambda i: sum(i['folders'].values()))}" if got else "Он нам: писем нет",
             f"Мы ему: {letters(sent)} — {by_box(lambda i: i['sent'])}" if sent else "Мы ему: не писали"]
    active = sorted(((mb, i) for mb, i in boxes.items() if i["folders"] or i["sent"]),
                    key=lambda x: (-(sum(x[1]["folders"].values()) + x[1]["sent"]), x[0]))
    for mb, i in active:
        n = sum(i["folders"].values())
        lines.append("")
        lines.append(f"📬 {mb}: от него {n}" + (f", от нас {i['sent']}" if i["sent"] else ""))
        lines += [f"  • {FOLDER_NAMES.get(f, f)} — {k}" for f, k in sorted(i["folders"].items(), key=lambda x: (-x[1], x[0]))]
        dates = []
        if i.get("last"):
            dates.append(f"от него: {i['first']} … {i['last']}" if i.get("first") and i["first"] != i["last"]
                         else f"от него: {i['last']}")
        if i.get("last_sent"):
            dates.append(f"наше последнее: {i['last_sent']}")
        if dates:
            lines.append("  " + " · ".join(dates))
    if not active:
        lines.append("Ни в одном ящике писем от него и к нему нет.")
    elif len(boxes) > len(active):
        lines.append(f"\nВ остальных ящиках ({len(boxes) - len(active)}) ничего.")
    if r.get("missing"):
        lines.append("⚠️ Не проверены (ящик недоступен): " + ", ".join(r["missing"]))
    return "\n".join(lines)


def _has_letters(r) -> bool:
    return any(i.get("letters") for i in r["boxes"].values())


def lookup_buttons(r, own_domains=(), confirm=False):
    """Кнопки под отчётом «где его письма»: сводка нейронкой (один раз) и решение спам / не спам (одно)."""
    rid = r["id"]
    if confirm:
        return [[("⚠️ Да, мы ему писали, но всё равно в спам", f"l:spam!:{rid}:0")], [("Отмена", f"l:back:{rid}:0")]]
    rows = []
    if not r.get("ai_at") and _has_letters(r):
        rows.append([("🧠 Сводка нейронкой", f"l:ai:{rid}:0")])
    if not r.get("decision"):
        row = []
        if domain_of(r["address"]) not in own_domains:
            row.append(("🚫 В спам", f"l:spam:{rid}:0"))
        row.append(("✅ Не спам → во входящие", f"l:ham:{rid}:0"))
        rows.append(row)
    return rows


def lookup_action(store, data: str, own_domains=()):
    """Спам / не спам под отчётом. -> (строка-итог к сообщению | None, короткий ответ, новые кнопки | None)"""
    try:
        _, action, ident, _ = data.split(":")
        ident = int(ident)
    except ValueError:
        return None, "Устаревшая кнопка", None
    r = store.get_lookup(ident)
    if not r:
        return None, "Отчёт не найден", None
    if r["decision"]:
        return None, "Уже решено", lookup_buttons(r, own_domains)
    if action == "back":
        return None, "Отменено", lookup_buttons(r, own_domains)
    if action in ("spam", "spam!"):
        if domain_of(r["address"]) in own_domains:
            return None, "Свой домен в спам не отправляю", lookup_buttons(r, own_domains)
        if action == "spam" and sum(i.get("sent", 0) for i in r["boxes"].values()):
            return None, "Мы ему писали — подтверди", lookup_buttons(r, own_domains, confirm=True)
        r, _ = store.decide_lookup(ident, "spam")
        return ("🚫 Решение: спам. Его письма во всех ящиках уйдут в спам, попробую отписаться от рассылки. "
                "Сводка придёт отдельным сообщением.", "В спам", lookup_buttons(r, own_domains))
    if action == "ham":
        r, _ = store.decide_lookup(ident, "ham")
        return ("✅ Решение: не спам. Спам-фильтр и серверная метка его больше не трогают, письма из спама "
                "во всех ящиках вернутся во входящие. Сводка придёт отдельным сообщением.", "Не спам",
                lookup_buttons(r, own_domains))
    return None, "Устаревшая кнопка", None


def lookup_ai(store, lookup_id: int) -> str:
    """Кнопка «🧠 Сводка нейронкой»: тексты последних писем по всем ящикам -> LLM. Возвращает текст сообщения."""
    r = store.get_lookup(lookup_id)
    if not r:
        return "Отчёт не найден."
    boxes = {mb: i["letters"] for mb, i in r["boxes"].items() if i.get("letters")}
    if not boxes:
        return "🧠 Текстов писем уже нет (храню пару дней) — пришли адрес ещё раз, соберу заново."
    text, err, used, dropped = summarize_correspondence(r["address"], boxes)
    if not text:
        return f"🧠 {r['address']}: нейронка не ответила — {err}. Цифры в отчёте выше актуальны."
    store.mark_lookup_ai(lookup_id)
    tail = f", ещё {dropped} не влезли в лимит" if dropped else ""
    return f"🧠 {r['address']} — сводка нейронкой (писем: {used}{tail})\n\n{text}"


def format_notice(n) -> str:
    who = f"{n['name']} <{n['sender']}>" if n.get("name") else n["sender"]
    lines = ["🛡 Почтовый сервер пометил письмо как спам — я убрал его в «Нежелательную почту».",
             f"От: {who}",
             f"Ящик: {n['mailbox']}" + (f" (таких писем: {n['letters']})" if n.get("letters", 1) > 1 else ""),
             f"Тема: {n['subject'] or '(без темы)'}"]
    if n.get("summary"):
        k = n.get("summarized", 1)
        lines.append(f"Кратко{f' (по {k} последним письмам)' if k > 1 else ''}: {n['summary']}")
    boxes = n.get("boxes") or {}
    sent = {mb: i.get("sent", 0) for mb, i in boxes.items() if i.get("sent")}
    lines.append("")
    if sent:
        lines.append("Переписка — мы ему писали:")
        lines += [f"  • {_short_box(mb)} — {k}" for mb, k in sorted(sent.items(), key=lambda x: -x[1])]
    else:
        lines.append("Переписки нет: ни из одного ящика мы ему не писали.")
    got = {mb: i for mb, i in boxes.items() if i.get("all")}
    if got:
        lines.append("Письма от него по ящикам:")
        for mb, i in sorted(got.items(), key=lambda x: -x[1]["all"]):
            sp = f" (в спаме {i['spam']})" if i.get("spam") else ""
            lines.append(f"  • {_short_box(mb)} — {i['all']}{sp}")
        rest = len(boxes) - len(got)
        if rest:
            lines.append(f"В остальных {rest} ящиках писем от него нет.")
    if n.get("missing"):
        lines.append("⚠️ Не проверены: " + ", ".join(n["missing"]))
    return "\n".join(lines)


def notice_buttons(notice_id):
    return [[("🚫 Оставить в спаме", f"n:keep:{notice_id}:0"), ("✅ Не спам → во входящие", f"n:rel:{notice_id}:0")]]


def notice_action(store, data: str):
    """Кнопка под уведомлением. -> (строка-итог для дописывания к сообщению, короткий ответ)"""
    try:
        _, action, ident, _ = data.split(":")
        ident = int(ident)
    except ValueError:
        return None, "Устаревшая кнопка"
    n, decision = store.decide_notice(ident, {"keep": "keep", "rel": "release"}.get(action, ""))
    if not n:
        return None, "Уведомление не найдено"
    if decision is None:
        was = "оставлен в спаме" if n["decision"] == "keep" else "возвращён во входящие"
        return f"Решение уже принято: {was}.", "Уже решено"
    if decision == "keep":
        return ("🚫 Решение: спам. Учусь — его письма во всех ящиках уйдут в спам, попробую отписаться. "
                "Сводка придёт отдельным сообщением.", "Оставлен в спаме")
    return ("✅ Решение: не спам. Серверная метка его больше не трогает, письма из спама во всех ящиках "
            "вернутся во входящие. Сводка придёт отдельным сообщением.", "Возвращаю во входящие")
