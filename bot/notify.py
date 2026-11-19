"""Сообщения бота: пуши админу, Excel-сводка, отправка текста мастеру.

Все текстовые сообщения идут через очередь (outbox): если Telegram недоступен,
они не теряются, а уходят, когда связь вернётся.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from . import outbox, textfmt
from .config import ADMIN_ID
from .db import db
from .excel import build_summary, excel_filename
from .runtime import rt
from .utils import day_totals, fmt_date, norm_name, plural, surname

log = logging.getLogger(__name__)


class TelegramUnavailable(Exception):
    """Telegram не отвечает — файл отправить не удалось (тексты при этом ждут в очереди)."""


def webapp_button(text: str = "📋 Открыть разнарядки") -> InlineKeyboardButton | None:
    if not rt.webapp_url:
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=rt.webapp_url))


def svod_keyboard(date_s: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📊 Свести разнарядки за {fmt_date(date_s)[:5]}", callback_data=f"svod:{date_s}")],
        [InlineKeyboardButton(text=f"📝 Свести текстом за {fmt_date(date_s)[:5]}", callback_data=f"svodtxt:{date_s}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dispatch_stats(data: dict) -> tuple[int, list[str]]:
    workers: set[int] = set()
    objs: list[str] = []
    for obj in data.get("objects", []):
        if obj.get("short") and obj["short"] not in objs:
            objs.append(obj["short"])
        for plan in obj.get("plans", []):
            workers.update(int(p["id"]) for p in plan.get("crew", []))
    return len(workers), objs


async def notify_admin_dispatch(dispatch: dict, is_update: bool) -> None:
    """Пуш админу о разнарядке. Никогда не роняет сохранение разнарядки."""
    try:
        date_s = dispatch["date"]
        n_workers, objs = dispatch_stats(dispatch["data"])
        all_for_date = await db.dispatches_for_date(date_s)
        names = ", ".join(surname(d["master_name"]) for d in all_for_date)
        verb = "обновил(а)" if is_update else "сделал(а)"
        itr = "ИТР выходит" if dispatch["data"].get("itr_on_work", True) else "ИТР не выходит"

        text = textfmt.normalize(dispatch["text"])
        if len(text) > 3000:
            text = text[:3000] + "\n…"
        msg = (
            f"📋 <b>{html.escape(dispatch['master_name'])}</b> {verb} разнарядку на <b>{fmt_date(date_s)}</b>\n"
            f"👷 {n_workers} {plural(n_workers, 'монтажник', 'монтажника', 'монтажников')} · {itr}"
            f" · {html.escape(' / '.join(objs) or '—')}\n"
            f"🗂 Всего на {fmt_date(date_s)[:5]}: {len(all_for_date)} — {html.escape(names)}\n"
            f"<blockquote expandable>{textfmt.to_html(text)}</blockquote>"
        )
        await outbox.send(ADMIN_ID, msg, reply_markup=svod_keyboard(date_s))
    except Exception:  # noqa: BLE001
        log.exception("Не удалось поставить пуш админу в очередь")


async def send_document_retry(chat_id: int, content: bytes, filename: str, caption: str) -> None:
    """Файл в очередь не кладём (он большой и быстро устаревает) — несколько попыток сразу."""
    bot = rt.bot
    assert bot is not None
    delays = (2, 5, 10)
    for attempt in range(len(delays) + 1):
        try:
            await bot.send_document(chat_id, BufferedInputFile(content, filename=filename), caption=caption)
            return
        except TelegramRetryAfter as e:
            if attempt == len(delays):
                raise TelegramUnavailable from e
            await asyncio.sleep(min(e.retry_after, 30))
        except (TelegramNetworkError, TelegramServerError) as e:
            if attempt == len(delays):
                raise TelegramUnavailable from e
            log.warning("Excel не ушёл (%s), повтор через %d с", type(e).__name__, delays[attempt])
            await asyncio.sleep(delays[attempt])


async def send_summary(chat_id: int, date_s: str) -> bool:
    """Собирает Excel по всем разнарядкам на дату и отправляет в чат.

    Возвращает False, если разнарядок нет. Если Telegram недоступен — TelegramUnavailable.
    """
    dispatches = await db.dispatches_for_date(date_s)
    if not dispatches:
        await outbox.send(chat_id, f"На {fmt_date(date_s)} пока нет ни одной разнарядки.")
        return False
    employees = await db.list_employees()
    content = build_summary(date_s, dispatches, employees)

    t = day_totals(dispatches, employees)
    submitted = {d["master_norm"] for d in dispatches}
    missing = [n for n in await db.recent_master_names(date_s) if norm_name(n) not in submitted]

    caption = (
        f"📊 <b>Сводная разнарядка на {fmt_date(date_s)}</b>\n"
        f"Мастеров: {len(dispatches)} — {html.escape(', '.join(d['master_name'] for d in dispatches))}\n"
        f"ИТР: {t['itr']} · Монтажников: {t['workers']} (вкл. водителей: {t['drivers']})"
    )
    if missing:
        caption += f"\n⚠️ Ещё не сдали: {html.escape(', '.join(missing))}"
    await send_document_retry(chat_id, content, excel_filename(date_s), caption[:1024])
    return True


async def send_text_to_user(chat_id: int, text: str) -> None:
    # Только жирные «Объект: …», больше никакого оформления — чтобы удобно копировать/пересылать
    for part in textfmt.chunks(textfmt.normalize(text)):
        await outbox.send(chat_id, textfmt.to_html(part))


async def send_text_summary(chat_id: int, date_s: str) -> bool:
    """Все разнарядки за дату одним текстом (отсутствующие — внизу). False, если разнарядок нет."""
    dispatches = await db.dispatches_for_date(date_s)
    if not dispatches:
        await outbox.send(chat_id, f"На {fmt_date(date_s)} пока нет ни одной разнарядки.")
        return False
    text = textfmt.merged_text(dispatches)
    parts = textfmt.chunks(text)
    for part in parts:
        await outbox.send(chat_id, textfmt.to_html(part))
    log.info("Текстовая сводка за %s: %d разнарядок, %d сообщ. → чат %s", date_s, len(dispatches), len(parts), chat_id)
    return True


async def notify_admin_access_request(user: dict) -> None:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "Без имени"
    uname = f"@{user['username']}" if user.get("username") else "без username"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Дать доступ", callback_data=f"acc:ok:{user['tg_id']}"),
        InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"acc:no:{user['tg_id']}"),
    ]])
    try:
        await outbox.send(
            ADMIN_ID,
            f"🆕 Запрос доступа к разнарядкам\n<b>{html.escape(name)}</b> ({html.escape(uname)}, id <code>{user['tg_id']}</code>)",
            reply_markup=kb,
        )
    except Exception:  # noqa: BLE001
        log.exception("Не удалось поставить запрос доступа в очередь")


async def notify_user_access(tg_id: int, granted: bool) -> None:
    try:
        if granted:
            # Без кнопки мини-аппа: адрес может смениться, пока сообщение ждёт в очереди.
            # Открыть приложение можно кнопкой «Разнарядка» в меню — она всегда актуальна.
            await outbox.send(tg_id, "✅ Доступ открыт! Теперь можно составлять разнарядки — "
                                     "кнопка «Разнарядка» слева от поля ввода.")
        else:
            await outbox.send(tg_id, "🚫 Администратор закрыл доступ к разнарядкам.")
    except Exception:  # noqa: BLE001
        log.exception("Не удалось поставить уведомление пользователю %s в очередь", tg_id)
