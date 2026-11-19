"""Журналы бота.

Всё лежит в data/logs/:
  bot.log       — всё подряд (запуск, действия пользователей, Telegram, ошибки)
  errors.log    — только предупреждения и ошибки: открывать первым
  access.log    — каждый запрос мини-аппа: кто, что, код ответа, сколько мс
  supervisor.log — перезапуски бота сторожем
  ngrok.log     — туннель
  reports/      — «Сообщить о проблеме» из мини-аппа (JSON с журналом действий на телефоне)

У каждой строки есть метка [rid]: номер запроса мини-аппа (6 символов) или u<номер> для
сообщений Telegram — по ней в логе находятся все строки одного запроса. Этот же номер
пользователь видит в сообщении «Ошибка сервера (код …)».
Токен бота в логи не попадает — он вырезается при записи.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import BASE_DIR, BOT_TOKEN, DATA_DIR

LOG_DIR = Path(os.environ.get("LOG_DIR") or DATA_DIR / "logs")
REPORTS_DIR = LOG_DIR / "reports"
STARTED = time.time()
rid_var: contextvars.ContextVar[str] = contextvars.ContextVar("rid", default="-")

_HASH_RE = re.compile(r"hash=[0-9a-f]{64}")


def redact(text: str) -> str:
    if BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "<BOT_TOKEN>")
        secret = BOT_TOKEN.split(":", 1)[-1]
        if len(secret) > 10:
            text = text.replace(secret, "<BOT_TOKEN>")
    return _HASH_RE.sub("hash=<…>", text)


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "rid"):
            record.rid = rid_var.get()
        return redact(super().format(record))


FORMAT = "%(asctime)s %(levelname)-7s [%(rid)s] %(name)s: %(message)s"


def _file(name: str, max_mb: int, backups: int, level: int = logging.INFO) -> RotatingFileHandler:
    h = RotatingFileHandler(LOG_DIR / name, maxBytes=max_mb * 1024 * 1024, backupCount=backups,
                            encoding="utf-8", delay=True)
    h.setLevel(level)
    h.setFormatter(_Formatter(FORMAT))
    return h


def code_version() -> str:
    """Короткий отпечаток кода (меняется при любой правке) + дата последней правки."""
    files = sorted([*(BASE_DIR / "bot").glob("*.py"), *(BASE_DIR / "webapp").glob("*.*")])
    h = hashlib.sha1()
    latest = 0.0
    for f in files:
        try:
            h.update(f.read_bytes())
            latest = max(latest, f.stat().st_mtime)
        except OSError:
            pass
    return f"{h.hexdigest()[:8]} от {time.strftime('%d.%m.%Y %H:%M', time.localtime(latest))}"


def _migrate_old_logs() -> None:
    """Старые логи из data/ — в data/logs/old/ (если файл не занят другим процессом)."""
    old = LOG_DIR / "old"
    # Только для настоящей папки логов: при LOG_DIR в другом месте (тесты) data/ не трогаем
    if LOG_DIR.resolve() != (DATA_DIR / "logs").resolve():
        return
    for f in DATA_DIR.glob("*.log"):
        try:
            if f.stat().st_size == 0:
                f.unlink()
                continue
            old.mkdir(parents=True, exist_ok=True)
            f.replace(old / f"{f.stem}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(f.stat().st_mtime))}.log")
        except OSError:
            pass  # занят (например, его ещё пишет старый ngrok) — заберём в следующий раз


def setup() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_old_logs()

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(_file("bot.log", 5, 5))
    root.addHandler(_file("errors.log", 2, 5, logging.WARNING))
    if sys.stderr and sys.stderr.isatty():  # запуск в окне (run.bat) — дублируем на экран
        sh = logging.StreamHandler()
        sh.setFormatter(_Formatter(FORMAT))
        root.addHandler(sh)

    access = logging.getLogger("access")
    access.propagate = False
    access.setLevel(logging.INFO)
    access.addHandler(_file("access.log", 5, 3))

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    def excepthook(exc_type, exc, tb):  # noqa: ANN001
        if not issubclass(exc_type, KeyboardInterrupt):
            logging.getLogger("bot").critical("Необработанная ошибка — бот падает", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = excepthook
    threading.excepthook = lambda a: logging.getLogger("bot").error(
        "Ошибка в потоке %s", a.thread.name if a.thread else "?", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def asyncio_exception_handler(loop, context: dict) -> None:  # noqa: ANN001
    exc = context.get("exception")
    logging.getLogger("bot").error("Ошибка в фоновой задаче: %s", context.get("message"),
                                   exc_info=(type(exc), exc, exc.__traceback__) if exc else None)


def who(user: dict | None) -> str:
    """«Гедаев У. (id 111111, @gedaev)» — для строк лога."""
    if not user:
        return "?"
    name = user.get("master_name") or " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or "без имени"
    uname = f", @{user['username']}" if user.get("username") else ""
    return f"{name} (id {user.get('tg_id')}{uname})"


def tail_text(path: Path, max_bytes: int) -> str:
    """Конец файла не больше max_bytes (с целой первой строкой)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read()
    except OSError as e:
        return f"<не прочитать {path.name}: {e}>"
    return data.decode("utf-8", errors="replace")
