"""Архив с логами для разработчика.

    python -m bot.logbundle      — собрать архив на Рабочий стол (работает, даже если бот не запускается)
    /logs в боте                 — бот пришлёт архив в чат
Внутри: 00_СВОДКА.txt (что сломалось — читать первым), system.txt, логи, сообщения о проблемах.
Токен бота вырезается.
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from .config import DATA_DIR, DB_PATH, EXCEL_SUFFIX, FIXED_ITR, PORT, TZ_OFFSET_HOURS, WEBAPP_URL
from .logs import LOG_DIR, REPORTS_DIR, STARTED, code_version, redact, tail_text

MB = 1024 * 1024
_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+)\s+\[([^\]]*)\] ([^:]+): (.*)$")


def _versions() -> str:
    out = []
    for mod in ("aiogram", "aiohttp", "aiosqlite", "openpyxl"):
        try:
            out.append(f"{mod} {__import__(mod).__version__}")
        except Exception:  # noqa: BLE001
            out.append(f"{mod} ?")
    return ", ".join(out)


def _db_stats() -> list[str]:
    lines = []
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as e:
        return [f"База не открывается: {e}"]
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    checks = [
        ("Пользователи по ролям", "SELECT role, COUNT(*) FROM users GROUP BY role"),
        ("Шифров", "SELECT COUNT(*) FROM ciphers"),
        ("Штатка по статусам", "SELECT status, COUNT(*) FROM employees WHERE deleted=0 GROUP BY status"),
        ("Разнарядок всего", "SELECT COUNT(*) FROM dispatches"),
        ("Разнарядки по датам (последние 10 дат)",
         "SELECT date, COUNT(*), GROUP_CONCAT(master_name, ', ') FROM dispatches GROUP BY date ORDER BY date DESC LIMIT 10"),
        ("Черновиков", "SELECT COUNT(*) FROM drafts"),
        ("Очередь сообщений (не ушло)", "SELECT COUNT(*), MIN(created_at), MAX(attempts) FROM outbox"),
    ]
    for title, sql in checks:
        try:
            rows = q(sql)
            if title.startswith("Очередь"):
                n, oldest, att = rows[0]
                lines.append(f"{title}: {n}, самое старое {int((time.time() - oldest) / 60)} мин назад, попыток до {att}"
                             if n else f"{title}: пусто")
            elif len(rows) == 1 and len(rows[0]) == 1:
                lines.append(f"{title}: {rows[0][0]}")
            else:
                lines.append(f"{title}:")
                lines.extend("    " + " | ".join(str(c) for c in r) for r in rows)
        except sqlite3.Error as e:
            lines.append(f"{title}: ошибка {e}")
    con.close()
    return lines


def _system(bot_info: dict | None) -> str:
    L = [
        f"Собрано: {datetime.now():%d.%m.%Y %H:%M:%S}",
        f"Версия кода: {code_version()}",
        f"Python {sys.version.split()[0]} · {platform.platform()} · {_versions()}",
    ]
    if bot_info:
        up = int(time.time() - STARTED)
        L.append(f"Бот работает {up // 3600} ч {up % 3600 // 60} мин, PID {os.getpid()}")
        L.extend(f"{k}: {v}" for k, v in bot_info.items())
    else:
        L.append("Собрано отдельной командой (python -m bot.logbundle), не из работающего бота")
    L += [
        "",
        "Настройки (.env, без секретов):",
        f"  PORT={PORT}  TZ_OFFSET_HOURS={TZ_OFFSET_HOURS}  EXCEL_SUFFIX={EXCEL_SUFFIX}",
        f"  WEBAPP_URL={WEBAPP_URL or '(пусто → ngrok)'}  NGROK_DOMAIN={os.environ.get('NGROK_DOMAIN') or '(пусто)'}"
        f"  USE_NGROK={os.environ.get('USE_NGROK', '1')}",
        f"  FIXED_ITR={FIXED_ITR}",
        "",
        "База:",
        *("  " + x for x in _db_stats()),
        "",
        "Копии базы: " + (", ".join(p.name for p in sorted((DATA_DIR / "backups").glob("*.sqlite3"))[-5:]) or "нет"),
        f"Свободно на диске: {shutil.disk_usage(DATA_DIR).free // MB} МБ",
        "",
        "Файлы логов:",
    ]
    for f in sorted(LOG_DIR.rglob("*")):
        if f.is_file():
            L.append(f"  {f.relative_to(LOG_DIR)}  {f.stat().st_size // 1024} КБ  "
                     f"{datetime.fromtimestamp(f.stat().st_mtime):%d.%m %H:%M}")
    return "\n".join(L)


def _summary(days: int = 7) -> tuple[str, int]:
    """Главное для разбора: ошибки за неделю (сгруппированы) и сообщения о проблемах."""
    since = datetime.now() - timedelta(days=days)
    day_ago = datetime.now() - timedelta(days=1)
    errors: list[tuple[datetime, str, str, str]] = []
    files = sorted(LOG_DIR.glob("errors.log*"), key=lambda p: p.stat().st_mtime)
    for f in files:
        cur: list | None = None  # [ts, level, rid, сообщение, причина из трассировки]
        for line in tail_text(f, 4 * MB).splitlines() + ["2000-01-01 00:00:00,0 END [-] x: x"]:
            m = _LINE_RE.match(line)
            if not m:
                # продолжение записи: трассировка; запоминаем последнюю строку вида «XxxError: …»
                if cur is not None and re.match(r"^[\w.]+(Error|Exception|Exit|Interrupt)\b", line.strip()):
                    cur[4] = line.strip()
                continue
            if cur is not None and cur[0] >= since and cur[1] in ("ERROR", "CRITICAL", "WARNING"):
                errors.append((cur[0], cur[1], cur[2], cur[3] + (f" → {cur[4]}" if cur[4] else "")))
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            cur = [ts, m.group(2), m.group(3), f"{m.group(4)}: {m.group(5)}", ""]
    serious = [e for e in errors if e[1] != "WARNING"]
    last_day = [e for e in serious if e[0] >= day_ago]

    L = [f"СВОДКА ДЛЯ РАЗБОРА · {datetime.now():%d.%m.%Y %H:%M}", "=" * 60, ""]
    L.append(f"Ошибок за сутки: {len(last_day)}, за {days} дн.: {len(serious)}; предупреждений за {days} дн.: "
             f"{len(errors) - len(serious)}")
    L.append("")

    reports = sorted(REPORTS_DIR.glob("*.json"))[-20:]
    if reports:
        L.append("СООБЩЕНИЯ О ПРОБЛЕМАХ (из мини-аппа, последние):")
        for p in reversed(reports):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
                L.append(f"  {r.get('at', '?')[:16]} · {r.get('who', '?')} · экран {r.get('view', '?')}")
                L.append("    «" + (r.get("text") or "").replace("\n", " ")[:300] + "»")
                L.append(f"    файл: reports/{p.name}")
            except Exception:  # noqa: BLE001
                L.append(f"  {p.name}: не прочитать")
        L.append("")

    if serious:
        groups: dict[str, list] = {}
        for e in serious:
            key = re.sub(r"\d+", "#", e[3])[:120]
            groups.setdefault(key, []).append(e)
        L.append("ОШИБКИ (сгруппированы, свежие сверху):")
        for key, items in sorted(groups.items(), key=lambda kv: kv[1][-1][0], reverse=True)[:30]:
            last = items[-1]
            L.append(f"  ×{len(items):<3} последняя {last[0]:%d.%m %H:%M} [{last[2]}] {last[1]}: {last[3][:240]}")
        L.append("  Подробности и трассировки — logs/errors.log (искать по [метке])")
        L.append("")

    warns = [e for e in errors if e[1] == "WARNING"][-25:]
    if warns:
        L.append("ПОСЛЕДНИЕ ПРЕДУПРЕЖДЕНИЯ:")
        L.extend(f"  {e[0]:%d.%m %H:%M} [{e[2]}] {e[3][:200]}" for e in warns)
        L.append("")
    if not errors and not reports:
        L.append("Ошибок и сообщений о проблемах нет.")
    return "\n".join(L), len(last_day)


def build(bot_info: dict | None = None) -> tuple[bytes, str, str]:
    """→ (zip, имя файла, короткая сводка для подписи)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    summary, n_errors = _summary()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name: str, text: str) -> None:
            z.writestr(name, redact(text))

        put("00_СВОДКА.txt", summary)
        put("system.txt", _system(bot_info))
        plan = [
            ("errors.log*", 4 * MB),
            ("bot.log", 6 * MB), ("bot.log.1", 3 * MB),
            ("access.log", 3 * MB),
            ("supervisor*.log", 512 * 1024),
            ("ngrok.log", 256 * 1024),
        ]
        for pattern, limit in plan:
            for f in sorted(LOG_DIR.glob(pattern)):
                put(f"logs/{f.name}", tail_text(f, limit))
        for f in sorted(REPORTS_DIR.glob("*.json"))[-50:]:
            put(f"reports/{f.name}", f.read_text(encoding="utf-8", errors="replace"))
        for f in sorted((LOG_DIR / "old").glob("*.log"))[-6:] + sorted(DATA_DIR.glob("*.log")):
            put(f"logs/old/{f.name}", tail_text(f, 512 * 1024))
    name = f"logs-razn-{datetime.now():%Y-%m-%d_%H%M}.zip"
    short = f"Ошибок за сутки: {n_errors}" if n_errors else "Ошибок за сутки нет"
    return buf.getvalue(), name, short


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    data, name, short = build()
    desktop = Path.home() / "Desktop"
    out_dir = desktop if desktop.is_dir() else DATA_DIR
    out = out_dir / name
    out.write_bytes(data)
    print(f"Готово: {out}  ({len(data) // 1024} КБ). {short}.")
    print("Перетащите этот файл в чат с Claude или перешлите его.")
    if sys.platform == "win32":
        subprocess.run(["explorer", f"/select,{out}"], check=False)


if __name__ == "__main__":
    main()
