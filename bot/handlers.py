"""Обработчики Telegram: /start, кнопки доступа и «Свести разнарядки»."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any, Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardMarkup, Message, Update

from . import logbundle, outbox
from .config import ADMIN_ID
from .db import db
from .logs import rid_var, who
from .notify import (
    TelegramUnavailable,
    notify_admin_access_request,
    notify_user_access,
    send_document_retry,
    send_summary,
    send_text_summary,
    webapp_button,
)
from .runtime import rt
from .utils import default_dispatch_date, fmt_date, today

router = Router()
log = logging.getLogger(__name__)


def _open_kb(text: str = "📋 Открыть разнарядки") -> InlineKeyboardMarkup | None:
    btn = webapp_button(text)
    return InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None


USER_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="app", description="Открыть разнарядки"),
]
ADMIN_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="app", description="Открыть мини-апп"),
    BotCommand(command="svod", description="Свести разнарядки (/svod 12.11.2026)"),
    BotCommand(command="svodtxt", description="Свести текстом (/svodtxt 12.11.2026)"),
    BotCommand(command="logs", description="Логи для разработчика (архив)"),
]


async def log_updates(handler: Callable[[Update, dict[str, Any]], Awaitable[Any]], event: Update,
                      data: dict[str, Any]) -> Any:
    """Каждое входящее сообщение/нажатие — строка в логе с меткой [u<номер>]."""
    token = rid_var.set(f"u{event.update_id}")
    try:
        if event.message and event.message.from_user:
            u = event.message.from_user
            text = (event.message.text or event.message.caption or f"<{event.message.content_type}>")[:120]
            log.info("Telegram: сообщение от %s (@%s): %r", u.id, u.username or "-", text)
        elif event.callback_query:
            u = event.callback_query.from_user
            log.info("Telegram: кнопка от %s (@%s): %s", u.id, u.username or "-", event.callback_query.data)
        return await handler(event, data)
    finally:
        rid_var.reset(token)


async def send_logs(chat_id: int) -> None:
    """Собрать архив логов и прислать в чат."""
    bot_info = {"Адрес мини-аппа": rt.webapp_url or "(нет)", "Сообщений в очереди": await db.outbox_count()}
    content, name, short = await asyncio.to_thread(logbundle.build, bot_info)
    log.info("Архив логов %s (%d КБ) — отправляю в чат %s", name, len(content) // 1024, chat_id)
    await send_document_retry(chat_id, content, name,
                              f"📦 Логи для разработчика. {short}.\nПерешлите этот файл в чат с Claude.")


NO_URL = "\n\n⚠️ Адрес мини-аппа ещё не настроен (нет HTTPS-туннеля). Смотрите README."


async def _answer(call: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Ответ на нажатие кнопки. Если связь пропадала и нажатие дошло поздно, Telegram отвечает
    «query is too old» — это не повод бросать само действие (сводку, выдачу доступа)."""
    try:
        await call.answer(text, show_alert=show_alert)
    except TelegramBadRequest:
        log.info("Нажатие дошло с опозданием, выполняю без всплывающего ответа")


async def _summary_safe(chat_id: int, date_s: str) -> None:
    try:
        await send_summary(chat_id, date_s)
    except TelegramUnavailable:
        await outbox.send(chat_id, f"⚠️ Не удалось отправить Excel за {fmt_date(date_s)} — пропадала связь с Telegram. "
                                   f"Нажмите «Свести разнарядки» ещё раз.")
    except Exception:  # noqa: BLE001
        log.exception("Сводка за %s не собралась", date_s)
        await outbox.send(chat_id, f"⚠️ Не получилось собрать сводку за {fmt_date(date_s)}. Попробуйте ещё раз.")


async def setup_admin_commands(bot: Bot) -> None:
    """Меню команд админа. Telegram примет его только после того, как админ нажал /start."""
    try:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except TelegramBadRequest:
        log.info("Меню админа поставлю после его /start (чат с ботом ещё не начат)")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    u = message.from_user
    user, created = await db.touch_user(u.id, u.username, u.first_name, u.last_name)
    role = user["role"]
    log.info("/start: %s, роль %s%s", who(user), role, " (новый пользователь)" if created else "")
    tail = "" if rt.webapp_url else NO_URL

    if role == "admin":
        await setup_admin_commands(message.bot)
        await message.answer(
            "👋 Вы — <b>администратор</b>.\n\n"
            "В мини-аппе: шифры, штатка, доступ мастеров, сводка и свои разнарядки.\n"
            "После каждой разнарядки сюда придёт пуш с кнопкой «Свести разнарядки» — она пришлёт Excel.\n\n"
            "Команда <code>/svod 12.11.2026</code> — свести за любую дату, "
            "<code>/svodtxt 12.11.2026</code> — то же одним текстом." + tail,
            reply_markup=_open_kb(),
        )
    elif role == "master":
        await message.answer(
            "👋 Здесь вы составляете разнарядку и одной кнопкой копируете её текст.\n"
            "Нажмите кнопку ниже или «Разнарядка» в меню." + tail,
            reply_markup=_open_kb(),
        )
    elif role == "blocked":
        await message.answer("🚫 Доступ к боту закрыт администратором.")
    else:
        if created:
            await notify_admin_access_request(user)
        await message.answer(
            "👋 Здравствуйте! Чтобы составлять разнарядки, нужен доступ.\n"
            "Заявка отправлена администратору — как только он подтвердит, я напишу."
        )


@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or user["role"] not in ("admin", "master"):
        await message.answer("Нет доступа. Нажмите /start, чтобы отправить заявку.")
        return
    await message.answer("Открыть мини-апп:" + ("" if rt.webapp_url else NO_URL), reply_markup=_open_kb())


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


def _parse_date_arg(arg: str | None) -> date | None:
    if not arg:
        return None
    arg = arg.strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", arg)
    if m:
        d, mth, y = m.groups()
        year = today().year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        return date(year, int(mth), int(d))
    try:
        return date.fromisoformat(arg)
    except ValueError:
        return None


@router.message(Command("svod"), F.from_user.id == ADMIN_ID)
async def cmd_svod(message: Message, command: CommandObject) -> None:
    try:
        d = _parse_date_arg(command.args) or default_dispatch_date()
    except ValueError:
        await message.answer("Не понял дату. Пример: <code>/svod 12.11.2026</code>")
        return
    await _summary_safe(message.chat.id, d.isoformat())


@router.message(Command("svodtxt"), F.from_user.id == ADMIN_ID)
async def cmd_svodtxt(message: Message, command: CommandObject) -> None:
    try:
        d = _parse_date_arg(command.args) or default_dispatch_date()
    except ValueError:
        await message.answer("Не понял дату. Пример: <code>/svodtxt 12.11.2026</code>")
        return
    await _text_summary_safe(message.chat.id, d.isoformat())


async def _text_summary_safe(chat_id: int, date_s: str) -> None:
    try:
        await send_text_summary(chat_id, date_s)
    except Exception:  # noqa: BLE001
        log.exception("Текстовая сводка за %s не собралась", date_s)
        await outbox.send(chat_id, f"⚠️ Не получилось собрать текстовую сводку за {fmt_date(date_s)}. Попробуйте ещё раз.")


@router.callback_query(F.data.startswith("svodtxt:"))
async def cb_svodtxt(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await _answer(call, "Только для администратора", show_alert=True)
        return
    date_s = call.data.split(":", 1)[1]
    await _answer(call, f"Свожу текстом за {fmt_date(date_s)}…")
    await _text_summary_safe(call.message.chat.id, date_s)


@router.message(Command("logs"), F.from_user.id == ADMIN_ID)
async def cmd_logs(message: Message) -> None:
    await message.answer("📦 Собираю логи…")
    await _logs_safe(message.chat.id)


@router.callback_query(F.data == "logs:send")
async def cb_logs(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await _answer(call, "Только для администратора", show_alert=True)
        return
    await _answer(call, "Собираю логи…")
    await _logs_safe(call.message.chat.id)


async def _logs_safe(chat_id: int) -> None:
    try:
        await send_logs(chat_id)
    except TelegramUnavailable:
        await outbox.send(chat_id, "⚠️ Архив логов не ушёл — пропадала связь с Telegram. Попробуйте ещё раз "
                                   "или запустите collect_logs.bat на компьютере с ботом.")
    except Exception:  # noqa: BLE001
        log.exception("Не удалось собрать архив логов")
        await outbox.send(chat_id, "⚠️ Не получилось собрать логи. Запустите collect_logs.bat на компьютере с ботом.")


@router.callback_query(F.data.startswith("svod:"))
async def cb_svod(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await _answer(call, "Только для администратора", show_alert=True)
        return
    date_s = call.data.split(":", 1)[1]
    await _answer(call, f"Свожу разнарядки за {fmt_date(date_s)}…")
    await _summary_safe(call.message.chat.id, date_s)


@router.callback_query(F.data.startswith("acc:"))
async def cb_access(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await _answer(call, "Только для администратора", show_alert=True)
        return
    _, action, uid_s = call.data.split(":")
    uid = int(uid_s)
    granted = action == "ok"
    await db.set_role(uid, "master" if granted else "blocked")
    log.info("Доступ: пользователь %s → %s (кнопкой в чате)", uid, "master" if granted else "blocked")
    await notify_user_access(uid, granted)
    await _answer(call, "Доступ открыт" if granted else "Заявка отклонена")
    note = "\n\n✅ Доступ выдан" if granted else "\n\n🚫 Отклонено"
    try:
        await call.message.edit_text(call.message.html_text + note, reply_markup=None)
    except Exception:  # noqa: BLE001
        pass


@router.message(F.chat.type == "private")
async def fallback(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    if user and user["role"] in ("admin", "master"):
        await message.answer("Разнарядки составляются в мини-аппе 👇", reply_markup=_open_kb())
    else:
        await message.answer("Нажмите /start")
