"""Сводная разнарядка в Excel — в формате «Наряд и расстановка персонала».

Лист 1 «Наряд задание»: мастер → объект → план → работы.
Лист 2 «Расстановка»: ИТР и весь персонал с объектом или статусом.
"""
from __future__ import annotations

import math
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import EXCEL_SUFFIX, FIXED_ITR
from .utils import STATUSES, day_totals, fmt_date, is_driver, parse_date, surname

FONT = Font(name="Calibri", size=8)
FONT_B = Font(name="Calibri", size=8, bold=True)
FONT_TITLE = Font(name="Calibri", size=10, bold=True)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN = Side(style="thin", color="000000")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FILL_HEAD = PatternFill("solid", fgColor="FFD9E1F2")
FILL_ABSENT = PatternFill("solid", fgColor="FFFFFF00")
FILL_FREE = PatternFill("solid", fgColor="FFF8CBAD")
FILL_CONFLICT = PatternFill("solid", fgColor="FFFFC000")

HEADERS = [
    "Бригада.", "Объект.", "Сооружение / здание.", "Проект. Раздел проекта.", "Наименование работ.",
    "Кол-во работников", "Ед. изм-я", "Объем, План.", "Строительная техника",
]
WIDTHS = [8.7, 6.9, 39.0, 23.4, 61.4, 8.4, 5.4, 9.1, 18.9]


def excel_filename(date_s: str) -> str:
    d = parse_date(date_s)
    suffix = f" {EXCEL_SUFFIX}" if EXCEL_SUFFIX else ""
    return f"Наряд и расстановка персонала на {d.strftime('%d.%m.%y')}{suffix}.xlsx"


def _num(value: str | None):
    if value is None:
        return None
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return str(value).strip()


def _cell(ws, row: int, col: int, value=None, font=FONT, fill=None):
    c = ws.cell(row=row, column=col, value=value)
    c.font = font
    c.alignment = CENTER
    c.border = BORDER
    if fill is not None:
        c.fill = fill
    return c


def _merge(ws, col: int, r1: int, r2: int) -> None:
    if r2 > r1:
        ws.merge_cells(start_row=r1, start_column=col, end_row=r2, end_column=col)


def _lines(text: str, chars_per_line: int) -> int:
    return max(1, math.ceil(len(text or "") / chars_per_line))


def build_summary(date_s: str, dispatches: list[dict], employees: list[dict]) -> bytes:
    wb = Workbook()
    _sheet_orders(wb.active, date_s, dispatches, day_totals(dispatches, employees))
    _sheet_people(wb.create_sheet("Расстановка"), dispatches, employees)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sheet_orders(ws, date_s: str, dispatches: list[dict], totals: dict) -> None:
    ws.title = "Наряд задание"
    for i, w in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=9)
    _cell(ws, 1, 1, f"Наряд-расстановка {fmt_date(date_s)}", FONT_TITLE)
    ws.row_dimensions[1].height = 16
    for col, h in enumerate(HEADERS, start=1):
        _cell(ws, 2, col, h, FONT_B, FILL_HEAD)
    ws.row_dimensions[2].height = 32.25

    row = 3
    for d in dispatches:
        data = d["data"]
        master_start = row
        objects = data.get("objects", [])
        if not objects:
            for col in range(2, 10):
                _cell(ws, row, col)
            _cell(ws, row, 5, "—")
            row += 1
        for obj in objects:
            obj_start = row
            for plan in obj.get("plans", []):
                plan_start = row
                works = plan.get("works", []) or [{"name": ""}]
                crew = plan.get("crew", [])
                for w in works:
                    vol = _num(w.get("volume"))
                    for col in (2, 3, 4, 6, 9):
                        _cell(ws, row, col)
                    _cell(ws, row, 5, (w.get("name") or "").strip())
                    _cell(ws, row, 7, (w.get("unit") or "").strip() if vol is not None else None)
                    _cell(ws, row, 8, vol)
                    ws.row_dimensions[row].height = max(22.5, _lines(w.get("name", ""), 82) * 11 + 6)
                    row += 1
                _cell(ws, plan_start, 6, len(crew) or None)
                _cell(ws, plan_start, 9, ", ".join(plan.get("tech", []) or []) or None)
                _merge(ws, 6, plan_start, row - 1)
                _merge(ws, 9, plan_start, row - 1)
            if row == obj_start:  # объект без планов — одна пустая строка
                for col in range(2, 10):
                    _cell(ws, row, col)
                row += 1
            _cell(ws, obj_start, 2, obj.get("short", ""))
            _cell(ws, obj_start, 3, obj.get("building", ""))
            code = obj.get("code", "")
            _cell(ws, obj_start, 4, code if code != obj.get("short") else None)
            for col in (2, 3, 4):
                _merge(ws, col, obj_start, row - 1)
        master_label = f"Мастер\n{surname(d['master_name'])}"
        if not data.get("itr_on_work", True):
            master_label += "\n(не выходит)"
        _cell(ws, master_start, 1, master_label)
        _merge(ws, 1, master_start, row - 1)

    bottom = (
        ("Водители", totals["drivers"]),
        ("Количество ИТР", totals["itr"]),
        ("Количество монтажников", totals["workers"]),  # водители уже внутри
    )
    for label, value in bottom:
        for col in range(1, 10):
            _cell(ws, row, col)
        _cell(ws, row, 5, label, FONT_B)
        _cell(ws, row, 6, value, FONT_B)
        ws.row_dimensions[row].height = 15
        row += 1


def _sheet_people(ws, dispatches: list[dict], employees: list[dict]) -> None:
    for col, w in zip("ABC", (4.5, 36.0, 30.0)):
        ws.column_dimensions[col].width = w
    ws.page_setup.orientation = "portrait"

    for col, h in enumerate(("№", "Ф.И.О.", "Объект."), start=1):
        _cell(ws, 1, col, h, FONT_B, FILL_HEAD)
    row = 2

    # ИТР — сначала постоянные (Марьин, СМБ), потом мастера, сдавшие разнарядку
    i = 0
    for name, place in FIXED_ITR:
        i += 1
        _cell(ws, row, 1, i)
        _cell(ws, row, 2, name, FONT_B)
        _cell(ws, row, 3, place)
        row += 1
    for d in dispatches:
        data = d["data"]
        objs: list[str] = []
        for obj in data.get("objects", []):
            s = obj.get("short", "")
            if s and s not in objs:
                objs.append(s)
        on_work = data.get("itr_on_work", True)
        i += 1
        _cell(ws, row, 1, i)
        _cell(ws, row, 2, d["master_name"], FONT_B)
        _cell(ws, row, 3, " / ".join(objs) if on_work else "Не выходит", fill=None if on_work else FILL_ABSENT)
        row += 1

    # Рабочие: кто куда поставлен
    placed: dict[int, list[str]] = {}
    masters_of: dict[int, set[str]] = {}
    snapshot_names: dict[int, str] = {}
    for d in dispatches:
        for obj in d["data"].get("objects", []):
            for plan in obj.get("plans", []):
                for p in plan.get("crew", []):
                    pid = int(p["id"])
                    snapshot_names[pid] = p.get("name", "")
                    lst = placed.setdefault(pid, [])
                    s = obj.get("short", "")
                    if s and s not in lst:
                        lst.append(s)
                    masters_of.setdefault(pid, set()).add(d["master_norm"])

    # Водители — первыми, как в исходной расстановке
    staff = sorted(employees, key=lambda e: not is_driver(e.get("position")))
    people = [(e["id"], e["full_name"], e["status"], e.get("position") or "") for e in staff]
    known = {e["id"] for e in employees}
    people += [(pid, snapshot_names[pid], "work", "") for pid in placed if pid not in known]

    for i, (pid, name, status, position) in enumerate(people, start=1):
        if pid in placed:
            value = " / ".join(placed[pid])
            fill = FILL_CONFLICT if len(masters_of.get(pid, ())) > 1 else None
        elif status != "work":
            value, fill = STATUSES.get(status, status), FILL_ABSENT
        elif is_driver(position):
            value, fill = position.strip(), None  # «Водитель» (или «Водитель Ford-048» из штатки)
        else:
            value, fill = "Не распределён", FILL_FREE
        _cell(ws, row, 1, i)
        _cell(ws, row, 2, name)
        _cell(ws, row, 3, value, fill=fill)
        row += 1

    row += 1
    legend = (
        (FILL_ABSENT, "БС / Отпуск / Межвахта / ИТР не выходит"),
        (FILL_FREE, "На работе, но нет ни в одной разнарядке"),
        (FILL_CONFLICT, "Стоит в разнарядках у разных мастеров"),
    )
    for fill, text in legend:
        _cell(ws, row, 1, None, fill=fill)
        c = ws.cell(row=row, column=2, value=text)
        c.font = Font(name="Calibri", size=8, italic=True)
        row += 1
