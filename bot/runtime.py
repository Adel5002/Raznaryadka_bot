"""Общее состояние процесса: бот и публичный адрес мини-аппа."""
from __future__ import annotations

from aiogram import Bot


class Runtime:
    bot: Bot | None = None
    webapp_url: str = ""


rt = Runtime()
