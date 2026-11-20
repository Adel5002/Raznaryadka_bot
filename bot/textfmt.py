"""Текст разнарядки для Telegram: жирные заголовки объектов, полосы между объектами,
текстовая сводка всех разнарядок за день.

Работает с готовым текстом разнарядки (его собирает мини-апп, buildText в app.js) —
поэтому в сводку попадает ровно то, что отправил мастер, а старые разнарядки
(с пустой строкой между объектами) приводятся к новому виду.
"""
from __future__ import annotations

import html
import re

SEP = "━" * 13          # под шапкой и перед «ИТОГО» — как было
OBJ_SEP = "━" * 16      # между объектами — чуть длиннее (держать в синхроне с OBJ_SEP в app.js)
HEADER = "РАЗНАРЯДКА:"
TOTALS = "ИТОГО НА РАБОТЕ:"
NOTE = "ПРИМЕЧАНИЕ:"
ABSENT_HEADERS = ("БС:", "ОТПУСК:", "МЕЖВАХТА:", "ВЫХОДНОЙ:")
OBJ_PREFIX = "Объект:"
CHUNK = 3800            # Telegram режет на 4096 символах — берём с запасом

_SEP_RE = re.compile(r"^━{5,}$")


def is_sep(line: str) -> bool:
    return bool(_SEP_RE.match(line.strip()))


def normalize(text: str) -> str:
    """Старый формат → новый: пустая строка перед «Объект:» становится полосой."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line.startswith(OBJ_PREFIX) and out and out[-1] == "" and len(out) >= 2 and not is_sep(out[-2]):
            out[-1] = OBJ_SEP
        out.append(line)
    return "\n".join(out)


def split_absent(text: str) -> tuple[str, dict[str, list[str]]]:
    """Вырезает блоки БС / ОТПУСК / МЕЖВАХТА (они идут после «ИТОГО», до «ПРИМЕЧАНИЕ»)."""
    lines = text.split("\n")
    found: dict[str, list[str]] = {h: [] for h in ABSENT_HEADERS}
    try:
        start = max(i for i, l in enumerate(lines) if l.strip() == TOTALS)
    except ValueError:
        return text, found
    out = lines[:start]
    i = start
    in_note = False
    while i < len(lines):
        line = lines[i]
        if line.strip() == NOTE:
            in_note = True
        if not in_note and line.strip() in ABSENT_HEADERS:
            header = line.strip()
            i += 1
            while i < len(lines) and lines[i].startswith("-"):
                name = lines[i][1:].strip()
                if name:
                    found[header].append(name)
                i += 1
            if out and out[-1] == "":
                out.pop()           # пустая строка перед блоком уходит вместе с ним
            continue
        out.append(line)
        i += 1
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out), found


def merged_text(dispatches: list[dict]) -> str:
    """Все разнарядки за день одним текстом: «РАЗНАРЯДКА:» один раз сверху,
    каждая разнарядка — как у мастера, а отсутствующие собраны в самом низу."""
    parts: list[str] = [HEADER]
    absent: dict[str, list[str]] = {h: [] for h in ABSENT_HEADERS}
    seen: dict[str, set[str]] = {h: set() for h in ABSENT_HEADERS}
    for n, d in enumerate(dispatches):
        body, found = split_absent(normalize(d.get("text") or ""))
        lines = body.split("\n")
        while lines and lines[0].strip() in ("", HEADER):
            lines.pop(0)
        if n:
            parts += ["", ""]
        parts += lines
        for h, names in found.items():
            for name in names:
                key = " ".join(name.lower().replace("ё", "е").split())
                if key not in seen[h]:
                    seen[h].add(key)
                    absent[h].append(name)
    if any(absent.values()):
        parts += ["", SEP]
        first = True
        for h in ABSENT_HEADERS:
            if not absent[h]:
                continue
            if not first:
                parts.append("")
            first = False
            parts.append(h)
            parts += ["-" + name for name in absent[h]]
    return "\n".join(parts)


def to_html(text: str) -> str:
    """Текст → HTML для Telegram: строки «Объект: …» жирные, остальное как есть."""
    out = []
    for line in text.split("\n"):
        esc = html.escape(line, quote=False)
        out.append(f"<b>{esc}</b>" if line.startswith(OBJ_PREFIX) else esc)
    return "\n".join(out)


def chunks(text: str, limit: int = CHUNK) -> list[str]:
    """Режет длинный текст на сообщения — по границам разнарядок и объектов, если получается."""
    lines = text.split("\n")
    result: list[str] = []
    cur: list[str] = []
    size = 0
    good = 0                # индекс в cur, перед которым удобно резать
    for line in lines:
        if cur and size + len(line) + 1 > limit:
            cut = good if good > 0 else len(cur)
            piece, cur = cur[:cut], cur[cut:]
            while piece and piece[-1] == "":
                piece.pop()
            result.append("\n".join(piece))
            while cur and cur[0] == "":
                cur.pop(0)
            size = sum(len(l) + 1 for l in cur)
            good = 0
        # удобные места: перед полосой между объектами и перед началом следующей разнарядки
        if cur and (line == OBJ_SEP or (line.startswith("Дата:") and cur[-1] == "")):
            good = len(cur)
        cur.append(line)
        size += len(line) + 1
        # одна строка длиннее лимита (огромное примечание) — режем грубо
        while size > limit and len(cur) == 1:
            result.append(cur[0][:limit])
            cur = [cur[0][limit:]]
            size = len(cur[0]) + 1
    if cur:
        result.append("\n".join(cur).rstrip("\n"))
    return [r for r in result if r.strip()]
