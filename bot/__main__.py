"""Точка входа: python -m bot

Поднимает веб-сервер мини-аппа, при необходимости — ngrok-туннель (HTTPS),
ставит кнопку меню «Разнарядка» и запускает бота.

Устойчивость:
- бот стартует и без связи с Telegram: мини-апп работает, сообщения копятся в очереди;
- сторож туннеля: если ngrok упал или сменил адрес — поднимает заново и обновляет кнопку меню;
- раз в сутки — копия базы в data/backups (хранится 14 последних);
- логи — в data/logs (см. bot/logs.py), архив для разработчика — /logs или collect_logs.bat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import urllib.request

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommandScopeChat, MenuButtonWebApp, WebAppInfo
from aiohttp import web

from . import logs, outbox
from .api import create_app
from .config import ADMIN_ID, BASE_DIR, BOT_TOKEN, DATA_DIR, HOST, PORT, WEBAPP_URL
from .db import db
from .handlers import ADMIN_COMMANDS, USER_COMMANDS, log_updates, router
from .runtime import rt
from .utils import today

log = logging.getLogger("bot")
NGROK_API = "http://127.0.0.1:4040/api/tunnels"
NGROK_LOG = logs.LOG_DIR / "ngrok.log"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = 14
WATCH_EVERY = 20  # сек — проверка туннеля и кнопки меню


# ── ngrok ─────────────────────────────────────────────────────────────────
def _ngrok_url_sync() -> str:
    try:
        with urllib.request.urlopen(NGROK_API, timeout=2) as resp:
            tunnels = json.load(resp).get("tunnels", [])
    except Exception:  # noqa: BLE001
        return ""
    for t in tunnels:
        addr = str(t.get("config", {}).get("addr", ""))
        if t.get("public_url", "").startswith("https://") and addr.endswith(f":{PORT}"):
            return t["public_url"].rstrip("/")
    return ""


class Tunnel:
    """ngrok-туннель, который бот поднимает и при необходимости перезапускает сам."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._out = None

    @staticmethod
    async def url() -> str:
        return await asyncio.to_thread(_ngrok_url_sync)

    @staticmethod
    def exe() -> str | None:
        # Своя копия в tools\ важнее системной: её можно обновить без прав админа (ngrok update)
        local = BASE_DIR / "tools" / ("ngrok.exe" if sys.platform == "win32" else "ngrok")
        return str(local) if local.exists() else shutil.which("ngrok")

    async def start(self) -> str:
        url = await self.url()
        if url:
            log.info("Нашёл уже запущенный ngrok: %s", url)
            return url
        exe = self.exe()
        if not exe:
            log.warning("WEBAPP_URL не задан и ngrok не найден — мини-апп не откроется. См. README.")
            return ""
        self.stop()
        cmd = [exe, "http", str(PORT), "--log", "stdout"]
        domain = os.environ.get("NGROK_DOMAIN", "").strip()
        if domain:
            cmd.insert(2, f"--url={domain}")
        log.info("Запускаю ngrok: %s", " ".join(cmd[1:]))
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._out = open(NGROK_LOG, "w", encoding="utf-8")  # noqa: SIM115 — живёт, пока жив ngrok
        self.proc = subprocess.Popen(cmd, stdout=self._out, stderr=subprocess.STDOUT, creationflags=flags)
        for _ in range(40):
            await asyncio.sleep(0.5)
            url = await self.url()
            if url:
                log.info("ngrok поднят: %s", url)
                return url
            if self.proc.poll() is not None:
                break
        self._out.flush()
        errors = [ln.strip() for ln in NGROK_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                  if "lvl=eror" in ln or "lvl=crit" in ln or "ERR_NGROK" in ln]
        log.warning("ngrok не дал адрес — мини-апп пока не откроется. Ошибка ngrok: %s (полный лог: %s)",
                    errors[-1] if errors else "нет", NGROK_LOG)
        return ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self._out:
            self._out.close()
            self._out = None


def _tunnel_mode() -> bool:
    return not WEBAPP_URL and os.environ.get("USE_NGROK", "1").strip() != "0"


async def keep_webapp_alive(bot: Bot, tunnel: Tunnel) -> None:
    """Следит, чтобы у мини-аппа был рабочий адрес и кнопка меню вела на него."""
    applied = ""  # адрес, который уже стоит на кнопке меню
    misses = 0    # сколько проверок подряд ngrok не отдал адрес
    while True:
        try:
            if _tunnel_mode():
                url = await tunnel.url()
                if url:
                    misses = 0
                else:
                    misses += 1
                    # Живой ngrok не трогаем из-за одного сбоя: перезапуск сменит адрес
                    if not tunnel.alive() or misses >= 3:
                        log.warning("Туннель ngrok пропал — поднимаю заново")
                        url = await tunnel.start()
                        misses = 0
            else:
                url = WEBAPP_URL
            if url and url != rt.webapp_url:
                if rt.webapp_url:
                    log.warning("Адрес мини-аппа сменился: %s → %s", rt.webapp_url, url)
                rt.webapp_url = url
            if url and url != applied:
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Разнарядка", web_app=WebAppInfo(url=url)))
                applied = url
                log.info("Мини-апп: %s (кнопка меню обновлена)", url)
        except Exception as e:  # noqa: BLE001 — нет связи с Telegram: попробуем в следующий раз
            log.warning("Не удалось обновить кнопку меню (%s) — повторю через %d с", type(e).__name__, WATCH_EVERY)
        await asyncio.sleep(WATCH_EVERY)


async def ensure_commands(bot: Bot) -> None:
    """Меню команд. Повторяет, пока не получится (на старте может не быть связи)."""
    done_default = done_admin = False
    delay = 5
    while not (done_default and done_admin):
        try:
            if not done_default:
                await bot.set_my_commands(USER_COMMANDS)
                done_default = True
            if not done_admin:
                try:
                    await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
                except TelegramBadRequest:
                    log.info("Меню админа поставлю после его /start (чат с ботом ещё не начат)")
                done_admin = True
        except Exception as e:  # noqa: BLE001
            log.warning("Меню команд не поставлено (%s), повтор через %d с", type(e).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


async def backups() -> None:
    while True:
        try:
            dest = BACKUP_DIR / f"bot-{today().isoformat()}.sqlite3"
            if not dest.exists():
                await db.backup(dest)
                log.info("Копия базы: %s", dest.name)
                old = sorted(BACKUP_DIR.glob("bot-*.sqlite3"))[:-BACKUP_KEEP]
                for p in old:
                    p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сделать копию базы")
        await asyncio.sleep(3600)


async def wait_for_telegram(bot: Bot) -> None:
    """Ждём связи с Telegram. Мини-апп всё это время уже работает."""
    delay = 3
    while True:
        try:
            me = await bot.get_me()
            log.info("Связь с Telegram есть: @%s", me.username)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("Нет связи с Telegram (%s) — мини-апп работает, сообщения копятся в очереди. "
                        "Повтор через %d с", type(e).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def main() -> None:
    logs.setup()
    asyncio.get_running_loop().set_exception_handler(logs.asyncio_exception_handler)
    log.info("════ Запуск бота · код %s · Python %s · %s · PID %s ════",
             logs.code_version(), platform.python_version(), platform.platform(terse=True), os.getpid())
    hosted = os.environ.get("HOSTED_ON", "").strip()
    if hosted:  # бот переехал на сервер: второй бот с тем же токеном мешал бы ему
        log.error("Бот работает на сервере (%s) — здесь не запускаю. Чтобы запустить на этом компьютере, "
                  "сначала остановите сервер (fly scale count 0 -a raznaryadka) и уберите строку HOSTED_ON из .env",
                  hosted)
        sys.exit(2)
    if not BOT_TOKEN:
        log.error("Не задан BOT_TOKEN в файле .env")
        sys.exit(2)

    await db.open()
    await db.touch_user(ADMIN_ID, None, None, None)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    rt.bot = bot

    # access_log=None: запросы пишет свой журнал (data/logs/access.log) — с пользователем и временем ответа
    runner = web.AppRunner(create_app(), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, HOST, PORT).start()
    except OSError as e:
        log.error("Порт %s занят (%s) — похоже, бот уже запущен в другом окне. Закройте его.", PORT, e)
        await db.close()
        await bot.session.close()
        sys.exit(3)
    log.info("Веб-сервер мини-аппа: http://localhost:%s", PORT)

    tunnel = Tunnel()
    if _tunnel_mode():
        rt.webapp_url = await tunnel.start()
    elif WEBAPP_URL:
        rt.webapp_url = WEBAPP_URL
    else:
        log.warning("USE_NGROK=0 и WEBAPP_URL пуст — бот работает, но мини-апп не откроется.")

    tasks = [
        asyncio.create_task(outbox.worker(), name="outbox"),
        asyncio.create_task(backups(), name="backups"),
        asyncio.create_task(ensure_commands(bot), name="commands"),
    ]
    if rt.webapp_url or _tunnel_mode():
        tasks.append(asyncio.create_task(keep_webapp_alive(bot, tunnel), name="webapp"))

    dp = Dispatcher()
    dp.update.outer_middleware(log_updates)
    dp.include_router(router)
    try:
        await wait_for_telegram(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Остановка бота")
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        await db.close()
        await bot.session.close()
        tunnel.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise  # код выхода (2 — «перезапуск не поможет») должен дойти до сторожа
    except BaseException:
        logging.getLogger("bot").critical("Бот упал", exc_info=True)
        raise
