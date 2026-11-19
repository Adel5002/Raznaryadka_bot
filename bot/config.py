"""Настройки бота.

ADMIN_ID зашит жёстко: этот Telegram-аккаунт всегда администратор,
независимо от того, что лежит в базе.
Остальное читается из файла .env в корне проекта.
"""
from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

# ── Жёстко зашитый системный администратор ────────────────────────────────
ADMIN_ID: int = 5645916737

BASE_DIR = Path(__file__).resolve().parent.parent
WEBAPP_DIR = BASE_DIR / "webapp"
DATA_DIR = BASE_DIR / "data"


def _load_env_file(path: Path) -> None:
    """Мини-парсер .env (KEY=VALUE), чтобы не тянуть лишние зависимости."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file(BASE_DIR / ".env")

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()
# Публичный HTTPS-адрес мини-аппа. Если пусто — бот попробует взять адрес
# у запущенного ngrok (http://127.0.0.1:4040).
WEBAPP_URL: str = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8080"))
# Часовой пояс площадки (для «сегодня/завтра» и запрета прошедших дат).
TZ_OFFSET_HOURS: float = float(os.environ.get("TZ_OFFSET_HOURS", "3"))
TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

DB_PATH: Path = Path(os.environ.get("DB_PATH", str(DATA_DIR / "bot.sqlite3")))
# Хвост имени Excel-файла: «Наряд и расстановка персонала на 12.11.26 <...>.xlsx»
EXCEL_SUFFIX: str = os.environ.get("EXCEL_SUFFIX", "общестрой СФЗ").strip()


def _parse_fixed_itr(raw: str) -> list[tuple[str, str]]:
    """«Марьин М.И.:СМБ; Иванов И.И.:91VTX» -> [("Марьин М.И.", "СМБ"), ...]"""
    result = []
    for part in raw.split(";"):
        name, _, place = part.partition(":")
        if name.strip():
            result.append((name.strip(), place.strip()))
    return result


# Постоянные ИТР: всегда первыми в блоке ИТР «Расстановки» и всегда в «Количестве ИТР».
FIXED_ITR: list[tuple[str, str]] = _parse_fixed_itr(os.environ.get("FIXED_ITR", "Марьин М.И.:СМБ"))
