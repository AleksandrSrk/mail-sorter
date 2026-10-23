"""Списки и действия для Telegram-бота. Без зависимостей от Telegram — чтобы тестировать.

Экран = (текст, ряды кнопок [[(подпись, callback_data), ...], ...]).
callback_data: "<список>:<действие>:<id>:<страница>"  (лимит Telegram — 64 байта)."""
from math import ceil

from .db import IN_FILTER, Store, now_iso
from .scoring import FREE, SUSPICIOUS, score_addr, score_domain

PAGE = 10
STATUS = {
    "active": "",
    "unsubscribing": "📭 отписались, ждём тишину",
    "unsubscribed": "✅ отписаны, сняты с фильтра",
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
    lines = [f"📧 Спам-адреса: {total}  (🗑 — убрать из фильтра, 🙈 — скрыть из списка)\n"]
    rows = []
    for i, it in enumerate(items, page * PAGE + 1):
        lines.append(f"{i}. {it['value']}{_mark(it)}")
        rows.append([(f"{i} 🗑 убрать", f"a:del:{it['id']}:{page}"), (f"{i} 🙈 скрыть", f"a:hide:{it['id']}:{page}")])
    return "\n".join(lines), rows + _nav("a", page, pages)


def domains(store, page=0):
    all_items = _domain_items(store, hidden=False)
    items, page, pages = _page(all_items, page)
    if not all_items:
        return "🌐 Спам-доменов нет.", []
    lines = [f"🌐 Спам-домены: {len(all_items)}  (🗑 — убрать домен и все его адреса из фильтра, "
             f"🙈 — скрыть, ➕ — блокировать весь домен)\n"]
    rows = []
    for i, it in enumerate(items, page * PAGE + 1):
        what = "весь домен в фильтре" if it["whole"] else f"адресов в фильтре: {it['n']}"
        lines.append(f"{i}. {it['value']} — {what}{_mark(it)}")
        row = [(f"{i} 🗑", f"d:del:{it['id']}:{page}"), (f"{i} 🙈", f"d:hide:{it['id']}:{page}")]
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
    lines = [f"🙈 Скрытые: {len(items)}  (🗑 — убрать из фильтра)\n"]
    rows = []
    for i, it in enumerate(part, page * PAGE + 1):
        label = it["value"] if it["t"] == "a" else f"домен {it['value']}"
        lines.append(f"{i}. {label}{_mark(it)}")
        rows.append([(f"{i} 🗑 убрать из фильтра", f"h:del{it['t']}:{it['id']}:{page}")])
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
