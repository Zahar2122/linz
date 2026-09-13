"""
Телеграм-бот "Расписание линз".

Функции:
- добавить пару линз с датой начала использования и интервалом замены
  (однодневные / двухнедельные / месячные / квартальные / свой интервал)
- посмотреть список линз и дату следующей замены
- удалить линзу
- настроить время ежедневного напоминания
- ежедневная проверка и рассылка напоминаний тем, у кого сегодня наступает
  (или уже наступила) дата замены
"""

import asyncio
import logging
import os
import re
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import database as db

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Скопируйте .env.example в .env и впишите токен."
    )

# Путь вебхука включает токен — это делает URL непредсказуемым для посторонних,
# т.к. по этому адресу Telegram будет присылать обновления боту.
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

router = Router()

INTERVAL_LABELS = {
    "daily": "Однодневные (каждый день)",
    "biweekly": "Двухнедельные (14 дней)",
    "monthly": "Месячные (30 дней)",
    "quarterly": "Квартальные (90 дней)",
    "custom": "Свой интервал",
}


# ---------- Состояния FSM ----------


class AddLens(StatesGroup):
    waiting_for_name = State()
    waiting_for_interval = State()
    waiting_for_custom_interval = State()
    waiting_for_start_date = State()


class SetReminderTime(StatesGroup):
    waiting_for_time = State()


# ---------- Вспомогательные функции ----------


def interval_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"interval:{key}")]
        for key, label in INTERVAL_LABELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def start_date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сегодня", callback_data="startdate:today")],
            [InlineKeyboardButton(text="Ввести дату вручную", callback_data="startdate:manual")],
        ]
    )


def format_lens_row(row) -> str:
    next_date = db.next_change_date(row["start_date"], row["interval_days"])
    days_left = (next_date - date.today()).days
    if days_left <= 0:
        status = "⚠️ Пора менять!"
    elif days_left == 1:
        status = "Завтра замена"
    else:
        status = f"Через {days_left} дн."
    return (
        f"#{row['id']} — <b>{row['name']}</b>\n"
        f"Интервал: {row['interval_days']} дн. | "
        f"Следующая замена: {next_date.strftime('%d.%m.%Y')} ({status})"
    )


# ---------- Команды ----------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    db.ensure_user(message.chat.id)
    await message.answer(
        "Привет! Я помогу не забыть вовремя поменять контактные линзы. 👁️\n\n"
        "Команды:\n"
        "/add — добавить пару линз\n"
        "/list — мои линзы и даты замены\n"
        "/delete — удалить линзу\n"
        "/settime — время ежедневного напоминания (по умолчанию 09:00)\n"
        "/help — показать это сообщение ещё раз"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.set_state(AddLens.waiting_for_name)
    await message.answer(
        "Как назовём эту пару линз? (например: «Дневные -2.5» или «Acuvue Oasys»)"
    )


@router.message(AddLens.waiting_for_name)
async def add_lens_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip()[:100])
    await state.set_state(AddLens.waiting_for_interval)
    await message.answer("Какой у линз интервал замены?", reply_markup=interval_keyboard())


@router.callback_query(AddLens.waiting_for_interval, F.data.startswith("interval:"))
async def add_lens_interval(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key == "custom":
        await state.set_state(AddLens.waiting_for_custom_interval)
        await callback.message.edit_text("Введите интервал замены в днях (число), например 7")
    else:
        interval_days = db.INTERVAL_PRESETS[key]
        await state.update_data(interval_days=interval_days)
        await state.set_state(AddLens.waiting_for_start_date)
        await callback.message.edit_text(
            "Когда вы начали (начнёте) носить эту пару?",
        )
        await callback.message.answer(
            "Выберите вариант:", reply_markup=start_date_keyboard()
        )
    await callback.answer()


@router.message(AddLens.waiting_for_custom_interval)
async def add_lens_custom_interval(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Нужно целое положительное число дней. Попробуйте ещё раз:")
        return
    await state.update_data(interval_days=int(text))
    await state.set_state(AddLens.waiting_for_start_date)
    await message.answer(
        "Когда вы начали (начнёте) носить эту пару?",
        reply_markup=start_date_keyboard(),
    )


@router.callback_query(AddLens.waiting_for_start_date, F.data == "startdate:today")
async def add_lens_start_today(callback: CallbackQuery, state: FSMContext) -> None:
    await finish_add_lens(callback.message, state, date.today(), callback.from_user.id)
    await callback.answer()


@router.callback_query(AddLens.waiting_for_start_date, F.data == "startdate:manual")
async def add_lens_start_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_text("Введите дату в формате ДД.ММ.ГГГГ, например 05.09.2026")
    await callback.answer()


@router.message(AddLens.waiting_for_start_date)
async def add_lens_start_date_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", text):
        await message.answer("Формат не распознан. Введите дату как ДД.ММ.ГГГГ, например 05.09.2026")
        return
    try:
        parsed = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Такой даты не существует. Попробуйте ещё раз, формат ДД.ММ.ГГГГ")
        return
    await finish_add_lens(message, state, parsed, message.chat.id)


async def finish_add_lens(message: Message, state: FSMContext, start_date: date, chat_id: int) -> None:
    data = await state.get_data()
    db.add_lens(chat_id, data["name"], start_date, data["interval_days"])
    next_date = db.next_change_date(start_date.isoformat(), data["interval_days"])
    await message.answer(
        f"Готово! Добавил «{data['name']}».\n"
        f"Следующая замена: {next_date.strftime('%d.%m.%Y')}.\n"
        f"Я напомню в день замены (время напоминания можно поменять через /settime)."
    )
    await state.clear()


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    rows = db.list_lenses(message.chat.id)
    if not rows:
        await message.answer("Пока нет ни одной пары линз. Добавьте через /add")
        return
    text = "\n\n".join(format_lens_row(row) for row in rows)
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("delete"))
async def cmd_delete(message: Message) -> None:
    rows = db.list_lenses(message.chat.id)
    if not rows:
        await message.answer("Удалять пока нечего — список пуст.")
        return
    buttons = [
        [InlineKeyboardButton(text=f"❌ {row['name']} (#{row['id']})", callback_data=f"del:{row['id']}")]
        for row in rows
    ]
    await message.answer(
        "Что удалить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("del:"))
async def delete_lens_callback(callback: CallbackQuery) -> None:
    lens_id = int(callback.data.split(":", 1)[1])
    deleted = db.delete_lens(callback.message.chat.id, lens_id)
    if deleted:
        await callback.message.edit_text("Удалено.")
    else:
        await callback.message.edit_text("Не нашёл такую запись (может, уже удалена).")
    await callback.answer()


@router.message(Command("settime"))
async def cmd_settime(message: Message, state: FSMContext) -> None:
    await state.set_state(SetReminderTime.waiting_for_time)
    current = db.get_user_reminder_time(message.chat.id)
    await message.answer(
        f"Сейчас напоминания приходят в {current}.\n"
        "Введите новое время в формате ЧЧ:ММ (например 08:30):"
    )


@router.message(SetReminderTime.waiting_for_time)
async def settime_input(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", text):
        await message.answer("Формат не подходит. Введите время как ЧЧ:ММ, например 08:30")
        return
    db.ensure_user(message.chat.id)
    db.set_reminder_time(message.chat.id, text)
    await message.answer(f"Готово! Теперь напоминания будут приходить в {text}.")
    await state.clear()


# ---------- Планировщик напоминаний ----------


async def send_reminders(bot: Bot) -> None:
    """Вызывается раз в минуту: проверяет, у кого сейчас наступило время напоминания."""
    now_str = datetime.now().strftime("%H:%M")
    chat_ids = db.get_all_users_with_reminder_time(now_str)
    today = date.today()
    for chat_id in chat_ids:
        rows = db.list_lenses(chat_id)
        due = []
        for row in rows:
            next_date = db.next_change_date(row["start_date"], row["interval_days"])
            already_notified = row["last_notified_date"] == next_date.isoformat()
            if next_date <= today and not already_notified:
                due.append(row)
                db.mark_notified(row["id"], next_date)
        if due:
            lines = "\n".join(f"• {row['name']}" for row in due)
            try:
                await bot.send_message(
                    chat_id,
                    f"🔔 Напоминание: сегодня пора поменять линзы:\n{lines}",
                )
            except Exception as exc:  # пользователь мог заблокировать бота и т.п.
                logger.warning("Не удалось отправить напоминание %s: %s", chat_id, exc)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp


async def run_polling() -> None:
    """Локальный запуск: бот сам опрашивает Telegram (удобно для разработки)."""
    bot = Bot(token=BOT_TOKEN)
    dp = create_dispatcher()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "cron", minute="*", args=[bot])
    scheduler.start()

    logger.info("Бот запущен в режиме polling")
    await dp.start_polling(bot)


def run_webhook(base_url: str, port: int) -> None:
    """
    Запуск через вебхук: Telegram сам присылает обновления HTTP-запросом.
    Используется при деплое на Render (или любой другой веб-хостинг).
    """
    bot = Bot(token=BOT_TOKEN)
    dp = create_dispatcher()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "cron", minute="*", args=[bot])

    app = web.Application()

    async def health_check(request: web.Request) -> web.Response:
        # Render (и сервисы вроде UptimeRobot) могут дёргать "/", чтобы
        # проверить, что сервис жив.
        return web.Response(text="Lens bot is running")

    app.router.add_get("/", health_check)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    async def on_startup(app: web.Application) -> None:
        webhook_url = f"{base_url}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        scheduler.start()
        logger.info("Бот запущен в режиме webhook: %s", webhook_url)

    async def on_shutdown(app: web.Application) -> None:
        await bot.delete_webhook()
        scheduler.shutdown(wait=False)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(app, host="0.0.0.0", port=port)


def main() -> None:
    db.init_db()
    # Render автоматически задаёт RENDER_EXTERNAL_URL — по его наличию
    # определяем, что бот запущен на хостинге, и включаем webhook-режим.
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        port = int(os.getenv("PORT", "10000"))
        run_webhook(base_url=render_url, port=port)
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
