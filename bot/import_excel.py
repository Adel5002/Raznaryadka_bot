"""Импорт шифров и штатки из старых Excel «Наряд и расстановка персонала».

    python -m bot.import_excel <файл.xlsx> [<файл2.xlsx> ...]

* Шифры (лист «Наряд задание», колонки «Сооружение» и «Проект») собираются со всех файлов.
* Штатка (лист «Расстановка», второй нумерованный блок — рабочие) берётся из самого
  свежего файла. Межвахта/Отпуск/БС переносятся как статусы.
Повторный запуск не плодит дубли: существующие шифры и ФИО пропускаются.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import openpyxl

from .db import db
from .utils import norm_name

STATUS_WORDS = {"межвахта": "mezhvahta", "отпуск": "otpusk", "бс": "bs"}
CIPHER_RE = re.compile(r"^[A-ZА-Я0-9]+\.[0-9A-Z]+\.[0-9A-Z]+\.", re.I)


def read_ciphers(path: Path) -> dict[str, str]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    found: dict[str, str] = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        building, code = (row[2] if len(row) > 2 else None), (row[3] if len(row) > 3 else None)
        if isinstance(code, str) and CIPHER_RE.match(code.strip()):
            found.setdefault(code.strip(), (building or "").strip() if isinstance(building, str) else "")
    return found


def read_staff(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    if "Расстановка" not in wb.sheetnames:
        return []
    ws = wb["Расстановка"]
    blocks: list[list[tuple[str, str]]] = []
    prev_num = None
    for row in ws.iter_rows(min_row=2, values_only=True):
        num, name, place = row[0], row[1], row[2] if len(row) > 2 else None
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(num, (int, float)) and (prev_num is None or num <= prev_num):
            blocks.append([])
        if not blocks:
            blocks.append([])
        blocks[-1].append((" ".join(name.split()), str(place or "").strip()))
        prev_num = num if isinstance(num, (int, float)) else prev_num
    workers = blocks[1] if len(blocks) > 1 else (blocks[0] if blocks else [])  # первый блок — ИТР
    result = []
    for name, place in workers:
        if "вакант" in name.lower():
            continue
        status = STATUS_WORDS.get(place.lower().strip(), "work")
        low = place.lower()
        position = "Водитель" if ("водитель" in low or "форд" in low or "ford" in low) else "Монтажник"
        result.append({"full_name": name, "position": position, "status": status})
    return result


async def main(paths: list[Path]) -> None:
    await db.open()
    ciphers: dict[str, str] = {}
    for p in paths:
        for code, building in read_ciphers(p).items():
            if building or code not in ciphers:
                ciphers[code] = building
    added, skipped = await db.add_ciphers([{"code": c, "building": b} for c, b in sorted(ciphers.items())])
    print(f"Шифры: добавлено {added}, уже были {len(skipped)}")

    newest = max(paths, key=lambda p: p.stat().st_mtime)
    staff = read_staff(newest)
    existing = {norm_name(e["full_name"]) for e in await db.list_employees()}
    fresh = [s for s in staff if norm_name(s["full_name"]) not in existing]
    n = await db.add_employees(fresh)
    print(f"Штатка (из «{newest.name}»): добавлено {n}, уже были {len(staff) - len(fresh)}")
    await db.close()


if __name__ == "__main__":
    files = [Path(a) for a in sys.argv[1:]]
    if not files:
        sys.exit(__doc__)
    asyncio.run(main(files))
