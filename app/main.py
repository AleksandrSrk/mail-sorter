"""Точка входа: цикл обхода ящиков."""
import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
import traceback

from .config import load_rules, load_settings
from .notifier import Notifier
from .sorter import MailboxSorter
from .state import LearnedSpam, StateStore

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


def main():
    settings = load_settings()
    setup_logging(settings.data_dir)
    log = logging.getLogger("main")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    notifier = Notifier(settings)
    socket.setdefaulttimeout(180)

    loaded = load_rules(settings.rules_file, list(settings.mailboxes))
    unknown = [mb for mb in loaded["boxes"] if mb not in settings.mailboxes]
    if unknown:
        log.warning("в правилах есть ящики без пароля в MAILBOXES: %s", ", ".join(unknown))
    learn = loaded["learn"]
    active = {mb: c for mb, c in loaded["boxes"].items() if mb in settings.mailboxes and (c["rules"] or learn)}
    state = StateStore(settings.data_dir)
    learned = LearnedSpam(settings.data_dir)
    log.info("старт: ящиков в env %d, в работе %d, интервал %d с, обучение спаму: %s, адресов в обученном списке %d",
             len(settings.mailboxes), len(active), settings.poll_interval,
             learn.folder if learn else "выкл", len(learned.items))
    for mb, conf in active.items():
        log.info("  %s: правил %d (%s)", mb, len(conf["rules"]), ", ".join(r.name for r in conf["rules"]))

    sorters = {
        mb: MailboxSorter(settings.imap_host, settings.imap_port, mb, settings.mailboxes[mb],
                          conf["rules"], conf["skip"], state.box(mb), learn=learn, learned=learned)
        for mb, conf in active.items()
    }
    notifier.send("start", "запущен",
                  "Сервис запущен.\n" + "\n".join(f"{mb}: {', '.join(r.name for r in c['rules'])}"
                                                   for mb, c in active.items()))
    failing = set()
    need_full = set(sorters)  # при старте — полный проход по всем ящикам
    while not STOP:
        started = time.time()
        today = time.strftime("%Y-%m-%d")
        if time.localtime().tm_hour == settings.full_scan_hour and state.data.get("last_full_day") != today:
            log.info("ночной полный проход")
            need_full = set(sorters)
            state.data["last_full_day"] = today
        learned.reload()
        for mb, sorter in sorters.items():
            if STOP:
                break
            full = mb in need_full
            try:
                t0 = time.time()
                moved, new_spam = sorter.run_once(full=full)
                state.save()
                if full:
                    need_full.discard(mb)
                    log.info("[%s] полный проход за %.0f с, перенесено %d", mb, time.time() - t0, moved)
                elif moved:
                    log.info("[%s] перенесено за проход: %d", mb, moved)
                if mb in failing:
                    failing.discard(mb)
                    log.info("[%s] снова работает", mb)
                    notifier.send(f"ok:{mb}", f"{mb}: снова работает", f"Ящик {mb} снова обрабатывается.", force=True)
            except Exception as e:
                state.save()
                failing.add(mb)
                log.error("[%s] ошибка: %s", mb, e)
                notifier.send(f"err:{mb}:{type(e).__name__}", f"ошибка в {mb}",
                              f"Ящик: {mb}\nОшибка: {e}\n\n{traceback.format_exc()}")
        heartbeat(settings.data_dir)
        delay = max(5, settings.poll_interval - (time.time() - started))
        while delay > 0 and not STOP:
            time.sleep(min(1, delay))
            delay -= 1
    log.info("остановлен")


if __name__ == "__main__":
    main()
