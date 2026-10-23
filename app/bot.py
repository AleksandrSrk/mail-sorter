"""Telegram-бот управления спам-фильтром. Отвечает только владельцу (TG_ADMIN_ID)."""
import asyncio
import logging
import os
import sys
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

from . import views
from .config import load_settings
from .db import Store

log = logging.getLogger("bot")
BUTTONS = {"📧 Спам-адреса": "a", "🌐 Спам-домены": "d", "🙈 Скрытые": "h", "📭 Отписанные": "u"}
ASK = {"🔎 Проверить адрес": "check"}
PROMPT = {"check": "Пришли адрес (можно несколько, можно прямо строкой из письма вроде «ООО Ромашка <info@x.ru>») — "
                   "покажу, в каких списках он есть, и дам кнопки убрать."}
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=t) for t in list(BUTTONS)[:2]], [KeyboardButton(text=t) for t in list(BUTTONS)[2:]],
              [KeyboardButton(text=t) for t in ASK]],
    resize_keyboard=True, is_persistent=True)


def markup(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in rows])


def build_router(store: Store, admin_id: int, own_domains=()) -> Router:
    r = Router()
    waiting = {}  # chat_id -> "check": ждём адрес после нажатия кнопки
    r.message.filter(F.from_user.id == admin_id)          # все остальные — тишина
    r.callback_query.filter(F.from_user.id == admin_id)

    @r.message(CommandStart())
    async def start(m: Message):
        await m.answer("Управление спам-фильтром почты. Ошибки сортировщика и отписки тоже приходят сюда.",
                       reply_markup=MENU)

    @r.message(F.text.in_(BUTTONS))
    async def show(m: Message):
        text, rows = views.SCREENS[BUTTONS[m.text]](store, 0)
        await m.answer(text, reply_markup=markup(rows))

    @r.message(F.text.in_(ASK))
    async def ask(m: Message):
        waiting[m.chat.id] = ASK[m.text]
        await m.answer(PROMPT[ASK[m.text]])

    @r.message(F.text)
    async def addresses_input(m: Message):
        mode = waiting.pop(m.chat.id, None)
        addrs = views.parse_addresses(m.text)
        if not addrs:
            await m.answer("Не вижу адреса почты. Нажми кнопку ещё раз и пришли адрес вида name@domain.ru.")
            return
        for a in addrs:  # кнопка «проверить» или просто прислали адрес — по сообщению на адрес, с кнопками
            text, rows = views.check_screen(store, a, own_domains)
            await m.answer(text[:4000], reply_markup=markup(rows) or MENU)

    @r.callback_query(F.data.startswith("x:rm:") | F.data.startswith("x:ex:"))
    async def restore(c: CallbackQuery):
        try:
            _, kind, ident, tail = c.data.split(":", 3)
            ident = int(ident)
        except ValueError:
            await c.answer("Устаревшая кнопка")
            return
        if kind == "ex":
            screen, note = views.exempt(store, ident, tail, own_domains)
        else:
            screen, note = views.restore(store, ident, own_domains)
        if screen:
            text, rows = screen
            try:
                await c.message.edit_text(text[:4000], reply_markup=markup(rows))
            except TelegramBadRequest:
                pass
        await c.answer(note)

    @r.callback_query(F.data == "noop")
    async def noop(c: CallbackQuery):
        await c.answer()

    @r.callback_query()
    async def action(c: CallbackQuery):
        screen, note = views.act(store, c.data or "")
        if screen is None:
            await c.answer("Устаревшая кнопка")
            return
        text, rows = screen
        try:
            await c.message.edit_text(text, reply_markup=markup(rows))
        except TelegramBadRequest:
            pass  # текст не изменился
        await c.answer(note)

    return r


async def heartbeat(path):
    while True:
        with open(path, "w") as f:
            f.write(str(int(time.time())))
        await asyncio.sleep(30)


async def run():
    s = load_settings()
    if not (s.tg_token and s.tg_admin):
        log.error("нет TG_BOT_TOKEN / TG_ADMIN_ID")
        sys.exit(1)
    store = Store(s.db_path)
    bot = Bot(s.tg_token)
    dp = Dispatcher()
    dp.include_router(build_router(store, s.tg_admin, s.own_domains))
    asyncio.create_task(heartbeat(os.path.join(s.data_dir, "bot_heartbeat")))
    log.info("бот запущен")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
