"""Сторож: python -m bot.supervisor

Запускает бота и перезапускает его, если он упал или завис
(перестал отвечать на http://127.0.0.1:PORT/health).
Паузы между перезапусками растут: 3 с, 6 с, … до минуты; после 5 минут
нормальной работы снова начинаются с 3 с.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

from .config import BASE_DIR, DATA_DIR, PORT

LOG_FILE = Path(os.environ.get("LOG_DIR") or DATA_DIR / "logs") / "supervisor.log"
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
HEALTH_EVERY = 30      # сек между проверками
HEALTH_FAILS = 4       # столько провалов подряд = бот завис
WARMUP = 90            # сек после старта, когда проверки не считаются
STABLE = 300           # сек работы, после которых пауза сбрасывается
FATAL_CODES = {2}      # нет токена — перезапуск не поможет


def say(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [сторож] {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:  # не растёт бесконечно
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout and sys.stdout.isatty():
        print(line, flush=True)


def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def run_once() -> tuple[int, float]:
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "bot"], cwd=str(BASE_DIR), env=os.environ.copy())
    say(f"бот запущен (PID {proc.pid})")
    fails = 0
    try:
        while True:
            try:
                code = proc.wait(timeout=HEALTH_EVERY)
                return code, time.monotonic() - started
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - started < WARMUP:
                continue
            if healthy():
                fails = 0
                continue
            fails += 1
            say(f"бот не отвечает ({fails}/{HEALTH_FAILS})")
            if fails >= HEALTH_FAILS:
                say("бот завис — перезапускаю")
                _kill_tree(proc)
                return -1, time.monotonic() - started
    except KeyboardInterrupt:
        _kill_tree(proc, graceful=True)
        raise


def _kill_tree(proc: subprocess.Popen, graceful: bool = False) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        if graceful:  # сервер (Fly) останавливает машину: даём боту закрыть базу и Telegram
            proc.terminate()
            try:
                proc.wait(12)
                return
            except subprocess.TimeoutExpired:
                say("бот не остановился за 12 с — снимаю принудительно")
        proc.kill()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass


def _on_sigterm(signum, frame) -> None:  # noqa: ANN001
    raise KeyboardInterrupt


def main() -> None:
    pause = 3
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _on_sigterm)
    say("старт")
    try:
        while True:
            code, uptime = run_once()
            if code in FATAL_CODES:
                say(f"бот завершился с кодом {code} — проверьте .env, перезапуск не поможет")
                sys.exit(code)
            if uptime > STABLE:
                pause = 3
            say(f"бот остановился (код {code}, проработал {int(uptime)} с) — перезапуск через {pause} с")
            time.sleep(pause)
            pause = min(pause * 2, 60)
    except KeyboardInterrupt:
        say("остановлен")


if __name__ == "__main__":
    main()
