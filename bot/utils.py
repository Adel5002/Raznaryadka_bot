from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import FIXED_ITR, TZ

STATUS_WORK = "work"
STATUSES: dict[str, str] = {
    "work": "На работе",
    "bs": "БС",
    "otpusk": "Отпуск",
    "mezhvahta": "Межвахта",
}
ABSENT_STATUSES = ("bs", "otpusk", "mezhvahta")


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def default_dispatch_date(base: date | None = None) -> date:
    """Следующий день; если это воскресенье — понедельник."""
    d = (base or today()) + timedelta(days=1)
    if d.weekday() == 6:  # воскресенье
        d += timedelta(days=1)
    return d


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def fmt_date(d: date | str) -> str:
    if isinstance(d, str):
        d = parse_date(d)
    return d.strftime("%d.%m.%Y")


def short_code(code: str) -> str:
    """AKU.0911.91VTX.CSB.AS.TB0004 -> 91VTX. Если формат другой — строка как есть."""
    code = (code or "").strip()
    parts = [p for p in code.split(".")]
    if len(parts) >= 3 and parts[2].strip():
        return parts[2].strip()
    return code


def surname(name: str) -> str:
    name = (name or "").strip()
    return name.split()[0] if name else ""


def norm_name(name: str) -> str:
    return " ".join((name or "").lower().replace("ё", "е").split())


def is_driver(position: str | None) -> bool:
    """Водитель — тот, у кого в штатке должность «Водитель…» (можно «Водитель Ford-048»)."""
    return "водител" in (position or "").lower()


def day_totals(dispatches: list[dict], employees: list[dict]) -> dict:
    """Итоги дня — одни и те же в Excel, в подписи к нему и в мини-аппе.

    ИТР = мастера, которые выходят + постоянные ИТР (Марьин).
    Водители = все водители из штатки со статусом «На работе» (по умолчанию всегда на линии).
    Монтажники = все из составов звеньев + водители (без двойного счёта).
    """
    crew: set[int] = set()
    for d in dispatches:
        for obj in d["data"].get("objects", []):
            for plan in obj.get("plans", []):
                crew.update(int(p["id"]) for p in plan.get("crew", []))
    drivers = {int(e["id"]) for e in employees if is_driver(e.get("position")) and e.get("status") == STATUS_WORK}
    masters_on = sum(1 for d in dispatches if d["data"].get("itr_on_work", True))
    return {
        "itr": masters_on + len(FIXED_ITR),
        "drivers": len(drivers),
        "workers": len(crew | drivers),
    }


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many
