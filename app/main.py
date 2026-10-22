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

    rules = load_rules(settings.rules_file)
    unknown = [mb for mb in rules if mb not in settings.mailboxes]
    if unknown:
        log.warning("в правилах есть ящики без пароля в MAILBOXES: %s", ", ".join(unknown))
    active = {mb: conf for mb, conf in rules.items() if mb in settings.mailboxes and conf["rules"]}
    log.info("старт: ящиков в env %d, с правилами %d, интервал %d с",
             len(settings.mailboxes), len(active), settings.poll_interval)
    for mb, conf in active.items():
        log.info("  %s: правил %d (%s)", mb, len(conf["rules"]), ", ".join(r.name for r in conf["rules"]))

    sorters = {
        mb: MailboxSorter(settings.imap_host, settings.imap_port, mb, settings.mailboxes[mb],
                          conf["rules"], conf["skip"])
        for mb, conf in active.items()
    }
    notifier.send("start", "запущен",
                  "Сервис запущен.\n" + "\n".join(f"{mb}: {', '.join(r.name for r in c['rules'])}"
                                                   for mb, c in active.items()))
    failing = set()
    while not STOP:
        started = time.time()
        for mb, sorter in sorters.items():
            if STOP:
                break
            try:
                moved = sorter.run_once()
                if moved:
                    log.info("[%s] перенесено за проход: %d", mb, moved)
                if mb in failing:
                    failing.discard(mb)
                    log.info("[%s] снова работает", mb)
                    notifier.send(f"ok:{mb}", f"{mb}: снова работает", f"Ящик {mb} снова обрабатывается.", force=True)
            except Exception as e:
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
