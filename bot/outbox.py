"""Надёжная отправка сообщений: сначала в базу, потом в Telegram.

Если связи с Telegram нет (обрыв интернета, блокировка у провайдера), сообщение
остаётся в очереди и уходит, как только связь вернётся — даже если бот за это время
перезапускался. Порядок сообщений сохраняется: при сетевой ошибке очередь ждёт.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup

from .db import db
from .runtime import rt

log = logging.getLogger(__name__)

MAX_AGE = 48 * 3600       # дольше двух суток не пытаемся — сообщение уже неактуально
IDLE_CHECK = 5            # как часто проверять очередь без «пинка», сек

_lock = asyncio.Lock()
_wake: asyncio.Event | None = None


def _event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def kick() -> None:
    """Разбудить отправщика (новое сообщение в очереди)."""
    _event().set()


async def send(chat_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None,
               parse_mode: str | None = "HTML") -> None:
    markup = reply_markup.model_dump_json(exclude_none=True) if reply_markup is not None else None
    await db.outbox_add(chat_id, text, parse_mode, markup)
    kick()


def _delay(attempts: int) -> float:
    return float(min(60, 5 * 2 ** min(attempts, 4)))  # 10 с, 20 с, 40 с, дальше раз в минуту


async def drain() -> int:
    """Отправить всё, что пора. Возвращает, сколько ушло."""
    bot = rt.bot
    if bot is None:
        return 0
    sent = 0
    async with _lock:
        for item in await db.outbox_due(time.time()):
            markup = InlineKeyboardMarkup.model_validate_json(item["markup"]) if item["markup"] else None
            try:
                await bot.send_message(item["chat_id"], item["text"], parse_mode=item["parse_mode"],
                                       reply_markup=markup)
            except TelegramRetryAfter as e:  # Telegram просит подождать
                await db.outbox_later(item["id"], item["attempts"] + 1, time.time() + e.retry_after + 1)
                break
            except (TelegramForbiddenError, TelegramNotFound, TelegramUnauthorizedError) as e:
                log.warning("Сообщение в чат %s не доставить (%s) — убираю из очереди", item["chat_id"], e)
                await db.outbox_done(item["id"])
            except TelegramBadRequest as e:
                if markup is not None and "button" in str(e).lower():
                    # кнопка устарела (например, сменился адрес мини-аппа) — шлём без неё
                    await db.c.execute("UPDATE outbox SET markup=NULL WHERE id=?", (item["id"],))
                    await db.c.commit()
                    continue
                log.warning("Telegram отклонил сообщение в чат %s (%s) — убираю из очереди", item["chat_id"], e)
                await db.outbox_done(item["id"])
            except Exception as e:  # noqa: BLE001 — сеть, 5xx: попробуем позже
                attempts = item["attempts"] + 1
                if time.time() - item["created_at"] > MAX_AGE:
                    log.error("Не смог отправить сообщение в чат %s за двое суток — удаляю", item["chat_id"])
                    await db.outbox_done(item["id"])
                    continue
                wait = _delay(attempts)
                log.warning("Нет связи с Telegram (%s). Сообщений в очереди ждут; повтор через %d с",
                            type(e).__name__, wait)
                await db.outbox_later(item["id"], attempts, time.time() + wait)
                break  # остальные тоже не уйдут — не долбим сеть и сохраняем порядок
            else:
                await db.outbox_done(item["id"])
                sent += 1
                first = item["text"].split("\n", 1)[0][:90]
                late = time.time() - item["created_at"]
                log.info("Доставлено в чат %s%s: %s", item["chat_id"],
                         f" (с опозданием {int(late)} с)" if late > 30 else "", first)
    return sent


async def worker() -> None:
    """Фоновый отправщик: работает всё время, пока жив бот."""
    pending = await db.outbox_count()
    if pending:
        log.info("В очереди с прошлого запуска: %d сообщ. — отправляю", pending)
    while True:
        _event().clear()
        try:
            await drain()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка отправщика сообщений")
        try:
            await asyncio.wait_for(_event().wait(), timeout=IDLE_CHECK)
        except asyncio.TimeoutError:
            pass
