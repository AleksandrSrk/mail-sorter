"""Точка входа: цикл обхода ящиков."""
import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
import traceback

from .config import load_settings
from .db import Store
from .notifier import Notifier
from .sorter import MailboxSorter
from .state import StateStore
from .unsub import unsubscribe
from .snippet import summarize_many
from .views import format_lookup, format_notice, lookup_buttons, notice_buttons

STOP = False


def _stop(*_):
    global STOP
    STOP = True


def setup_logging(data_dir):
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h1 = logging.StreamHandler(sys.stdout)
    h1.setFormatter(fmt)
    h2 = logging.handlers.RotatingFileHandler(
        os.path.join(data_dir, "logs", "mail-sorter.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h2.setFormatter(fmt)
    root.handlers = [h1, h2]


def heartbeat(data_dir):
    with open(os.path.join(data_dir, "heartbeat"), "w") as f:
        f.write(str(int(time.time())))


def process_unsubs(store, settings, notifier, log):
    """Очередь отписок + перевод в «отписанные» после недели тишины."""
    try:
        for row in store.pending_unsubs():
            ok, result = unsubscribe(row, settings)
            store.finish_unsub(row["id"], ok, result)
            log.info("отписка от %s (%s): %s", row["sender"], row["mailbox"], result)
            if store.has_open_report(row["sender"]):
                continue  # результат уйдёт в общем отчёте по адресу
            notifier.send(f"unsub:{row['id']}", ("📭 отписался от " if ok else "⚠️ не смог отписаться от ")
                          + row["sender"], f"Ящик: {row['mailbox']}\n{result}", force=True)
        for sender in store.expire_unsubscribing(settings.unsub_quiet_days):
            log.info("%s: неделя тишины, снят с фильтра", sender)
            notifier.send(f"quiet:{sender}", f"🔕 {sender}: тишина {settings.unsub_quiet_days} дн. после отписки — "
                          "снят со спам-фильтра, лежит в «Отписанных»", force=True)
    except Exception as e:
        log.error("ошибка в обработке отписок: %s", e)
        notifier.send(f"err:unsub:{type(e).__name__}", "❌ ошибка в отписках", f"{e}\n\n{traceback.format_exc()[-2500:]}")


def format_report(r) -> str:
    spam = r.get("spam", True)
    folder = r.get("folder", "Нежелательная почта")
    if spam:
        head = (f"↩️ Снова в спам-фильтре: {r['sender']}" if r["action"] == "returned"
                else f"🚫 В спам-фильтр добавлен: {r['sender']}")
        where, into = "Кинули в спам в ящике", "в спам"
    else:
        head = f"📁 {folder}: {'снова ' if r['action'] == 'returned' else ''}добавлен {r['sender']}"
        where, into = f"Положили в «{folder}» в ящике", f"в «{folder}»"
    lines = [head, f"{where}: {r['mailbox']}", ""]
    moved = {mb: n for mb, n in r["boxes"].items() if n}
    total = sum(moved.values())
    if total:
        lines.append(f"Старые письма перенесены {into}: {total}")
        lines += [f"  • {mb} — {n}" for mb, n in sorted(moved.items(), key=lambda x: -x[1])]
        rest = len(r["boxes"]) - len(moved)
        if rest:
            lines.append(f"В остальных {rest} ящиках писем от него не было.")
    else:
        lines.append(f"Других писем от него ни в одном из {len(r['boxes'])} ящиков не было.")
    if r["missing"]:
        lines.append("⚠️ Не проверены (ящик недоступен): " + ", ".join(r["missing"]))
    if not spam:
        return "\n".join(lines)
    lines.append("")
    if r["no_unsub"]:
        lines.append("Отписка: не делаем — от него уже отписывались, теперь он в фильтре навсегда.")
    elif not r["unsubs"]:
        lines.append("Отписка: ➖ в письме нет заголовка для отписки — автоматически нельзя.")
    else:
        lines.append("Отписка:")
        for u in r["unsubs"]:
            mark = {"done": "✅", "failed": "❌"}.get(u["status"], "⏳")
            lines.append(f"  {mark} {u['mailbox']} — {u['result'] or 'в очереди'}")
    return "\n".join(lines)


def send_reports(store, settings, notifier, log):
    """Сводка по каждому новому адресу: сколько старых писем и где перенесено, чем кончилась отписка."""
    try:
        for r in store.ready_reports(list(settings.mailboxes)):
            text = format_report(r)
            log.info("отчёт: %s", text.replace("\n", " | "))
            if notifier.send(f"report:{r['id']}", text, force=True) or not notifier.enabled:
                store.close_report(r["id"])
    except Exception as e:
        log.error("ошибка в отчётах: %s", e)
        notifier.send(f"err:report:{type(e).__name__}", "❌ ошибка в отчётах", f"{e}\n\n{traceback.format_exc()[-2500:]}")


def format_restore(r) -> str:
    total = sum(r["boxes"].values())
    lines = [f"↩️ {r['needle']} убран из «{r['rule_name']}»"]
    if total:
        lines.append(f"Во входящие возвращено писем: {total}")
        lines += [f"  • {mb} — {n}" for mb, n in sorted(r["boxes"].items(), key=lambda x: -x[1]) if n]
    else:
        lines.append(f"В папке «{r['folder']}» его писем не было — возвращать нечего.")
    if r["missing"]:
        lines.append("⚠️ Не проверены (ящик недоступен): " + ", ".join(r["missing"]))
    return "\n".join(lines)


def send_restores(store, settings, notifier, log):
    try:
        for r in store.ready_restores(list(settings.mailboxes)):
            text = format_restore(r)
            log.info("возврат: %s", text.replace("\n", " | "))
            if notifier.send(f"restore:{r['id']}", text, force=True) or not notifier.enabled:
                store.close_restore(r["id"])
    except Exception as e:
        log.error("ошибка в возвратах: %s", e)
        notifier.send(f"err:restore:{type(e).__name__}", "❌ ошибка при возврате писем",
                      f"{e}\n\n{traceback.format_exc()[-2500:]}")


def send_notices(store, settings, notifier, log):
    """Серверный спам: когда все ящики посчитали письма отправителя — уведомление с выбором."""
    try:
        for n in store.ready_notices(list(settings.mailboxes)):
            n["summary"] = summarize_many(n["texts"]) or n["summary"]
            n["summarized"] = len([x for x in n["texts"] if x["text"].strip()][:5])
            if notifier.send(f"notice:{n['id']}", format_notice(n), force=True,
                             buttons=notice_buttons(n["id"])) or not notifier.enabled:
                store.mark_notice_sent(n["id"])
    except Exception as e:
        log.error("ошибка в уведомлениях о серверном спаме: %s", e)
        notifier.send(f"err:notice:{type(e).__name__}", "❌ ошибка в уведомлениях о серверном спаме",
                      f"{e}\n\n{traceback.format_exc()[-2500:]}")


def send_lookups(store, settings, notifier, log):
    """Отчёт «где его письма» — когда все ящики посмотрели свои папки."""
    try:
        store.purge_lookup_letters()
        for r in store.ready_lookups(list(settings.mailboxes)):
            buttons = lookup_buttons(r, settings.own_domains)
            if notifier.send(f"lookup:{r['id']}", format_lookup(r), force=True, buttons=buttons) \
                    or not notifier.enabled:
                store.close_lookup(r["id"])
    except Exception as e:
        log.error("ошибка в отчёте по адресу: %s", e)
        notifier.send(f"err:lookup:{type(e).__name__}", "❌ ошибка в отчёте «где его письма»",
                      f"{e}\n\n{traceback.format_exc()[-2500:]}")


def main():
    settings = load_settings()
    setup_logging(settings.data_dir)
    log = logging.getLogger("main")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    notifier = Notifier(settings)
    socket.setdefaulttimeout(180)

    store = Store(settings.db_path)
    if store.seed(settings.rules_file, os.path.join(settings.data_dir, "learned_spam.txt")):
        log.info("правила перенесены в базу из %s", settings.rules_file)
    state = StateStore(settings.data_dir)
    rules_count = store.one("SELECT count(*) AS n FROM rules WHERE enabled=1")["n"]
    log.info("старт: ящиков %d, правил в базе %d, интервал %d с, уведомления в Telegram: %s",
             len(settings.mailboxes), rules_count, settings.poll_interval, "да" if notifier.enabled else "НЕТ")

    sorters = {
        mb: MailboxSorter(settings.imap_host, settings.imap_port, mb, pw, store, state.box(mb),
                          own_domains=settings.own_domains)
        for mb, pw in settings.mailboxes.items()
    }
    notifier.send("start", "запущен", f"Ящиков: {len(sorters)}, правил: {rules_count}")
    failing = set()
    need_full = set(sorters)  # при старте — полный проход по всем ящикам
    while not STOP:
        started = time.time()
        today = time.strftime("%Y-%m-%d")
        if time.localtime().tm_hour == settings.full_scan_hour and state.data.get("last_full_day") != today:
            log.info("ночной полный проход")
            need_full = set(sorters)
            state.data["last_full_day"] = today
        for mb, sorter in sorters.items():
            if STOP:
                break
            full = mb in need_full
            try:
                t0 = time.time()
                moved, events = sorter.run_once(full=full)
                state.save()
                if full:
                    need_full.discard(mb)
                    log.info("[%s] полный проход за %.0f с, перенесено %d", mb, time.time() - t0, moved)
                elif moved:
                    log.info("[%s] перенесено за проход: %d", mb, moved)
                if mb in failing:
                    failing.discard(mb)
                    log.info("[%s] снова работает", mb)
                    notifier.send(f"ok:{mb}", f"✅ {mb}: снова работает", force=True)
            except Exception as e:
                state.save()
                failing.add(mb)
                log.error("[%s] ошибка: %s", mb, e)
                notifier.send(f"err:{mb}:{type(e).__name__}", f"❌ ошибка в {mb}",
                              f"{e}\n\n{traceback.format_exc()[-2500:]}")
        if not STOP:
            process_unsubs(store, settings, notifier, log)
            send_reports(store, settings, notifier, log)
            send_restores(store, settings, notifier, log)
            send_notices(store, settings, notifier, log)
            send_lookups(store, settings, notifier, log)
        heartbeat(settings.data_dir)
        delay = max(5, settings.poll_interval - (time.time() - started))
        while delay > 0 and not STOP:
            time.sleep(min(1, delay))
            delay -= 1
    log.info("остановлен")


if __name__ == "__main__":
    main()
