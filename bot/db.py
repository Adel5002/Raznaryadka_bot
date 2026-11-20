"""SQLite-хранилище: пользователи, шифры, штатка, разнарядки, черновики."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .config import ADMIN_ID, DB_PATH
from .utils import ABSENT_STATUSES, STATUSES, norm_name, now, short_code

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id        INTEGER PRIMARY KEY,
    username     TEXT,
    first_name   TEXT,
    last_name    TEXT,
    master_name  TEXT,
    role         TEXT NOT NULL DEFAULT 'pending',  -- admin | master | pending | blocked
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ciphers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    building    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name          TEXT NOT NULL,
    position           TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'work',
    status_changed_at  TEXT,
    status_changed_by  INTEGER,
    deleted            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id    INTEGER NOT NULL,
    date         TEXT NOT NULL,          -- YYYY-MM-DD
    master_name  TEXT NOT NULL,
    master_norm  TEXT NOT NULL,
    data         TEXT NOT NULL,          -- JSON
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (author_id, date, master_norm)
);
CREATE INDEX IF NOT EXISTS ix_dispatches_date ON dispatches(date);
CREATE INDEX IF NOT EXISTS ix_dispatches_author ON dispatches(author_id, date);

CREATE TABLE IF NOT EXISTS drafts (
    author_id   INTEGER PRIMARY KEY,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Очередь исходящих сообщений: если Telegram недоступен, сообщение ждёт здесь и уходит позже
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    parse_mode  TEXT,
    markup      TEXT,                     -- JSON reply_markup
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     REAL NOT NULL,            -- unix time
    created_at  REAL NOT NULL
);
"""


def _ts() -> str:
    return now().isoformat(timespec="seconds")


IMPORT_PATH = DB_PATH.parent / "import.sqlite3"


def _import_pending() -> None:
    """Подмена базы при запуске: если рядом лежит import.sqlite3 (перенос на сервер,
    восстановление из копии) — текущая база уходит в backups/, import.sqlite3 становится базой.
    Вызывается до открытия базы, когда её никто не держит."""
    if not IMPORT_PATH.exists():
        return
    log = logging.getLogger("bot.db")
    try:
        con = sqlite3.connect(f"file:{IMPORT_PATH}?mode=ro", uri=True)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        if ok != "ok" or not {"users", "dispatches"} <= tables:
            raise ValueError(f"проверка: {ok}, таблицы: {sorted(tables)}")
    except Exception as e:  # noqa: BLE001
        bad = IMPORT_PATH.with_name(f"import-bad-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
        IMPORT_PATH.replace(bad)
        log.error("import.sqlite3 не подходит (%s) — база не заменена, файл отложен: %s", e, bad.name)
        return
    if DB_PATH.exists():
        keep = DB_PATH.parent / "backups" / f"before-import-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
        keep.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(keep)
        try:
            src.backup(dst)  # вместе с несброшенным WAL
        finally:
            dst.close()
            src.close()
        log.warning("Прежняя база сохранена: backups/%s", keep.name)
    for suffix in ("-wal", "-shm"):
        Path(f"{DB_PATH}{suffix}").unlink(missing_ok=True)
        Path(f"{IMPORT_PATH}{suffix}").unlink(missing_ok=True)  # остаются от проверки файла
    IMPORT_PATH.replace(DB_PATH)
    log.warning("База заменена файлом import.sqlite3")


class DB:
    def __init__(self) -> None:
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_import_pending)
        self.conn = await aiosqlite.connect(DB_PATH, timeout=15)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")  # с WAL: надёжно и быстро
        await self.conn.execute("PRAGMA busy_timeout=15000")  # подождать, если база занята, а не падать
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def backup(self, dest: Path) -> None:
        """Целостная копия базы на ходу. Отдельное соединение и backup API SQLite:
        не мешает запросам бота (VACUUM INTO на общем соединении падает, если идёт другой запрос)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")

        def run() -> None:
            tmp.unlink(missing_ok=True)
            src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            tmp.replace(dest)

        await asyncio.to_thread(run)

    # ── очередь сообщений ────────────────────────────────────────────────
    async def outbox_add(self, chat_id: int, text: str, parse_mode: str | None, markup: str | None) -> int:
        t = time.time()
        cur = await self.c.execute(
            "INSERT INTO outbox (chat_id, text, parse_mode, markup, next_at, created_at) VALUES (?,?,?,?,?,?)",
            (chat_id, text, parse_mode, markup, t, t),
        )
        await self.c.commit()
        return cur.lastrowid

    async def outbox_due(self, at: float, limit: int = 50) -> list[dict]:
        cur = await self.c.execute("SELECT * FROM outbox WHERE next_at<=? ORDER BY id LIMIT ?", (at, limit))
        return [dict(r) for r in await cur.fetchall()]

    async def outbox_done(self, item_id: int) -> None:
        await self.c.execute("DELETE FROM outbox WHERE id=?", (item_id,))
        await self.c.commit()

    async def outbox_later(self, item_id: int, attempts: int, next_at: float) -> None:
        await self.c.execute("UPDATE outbox SET attempts=?, next_at=? WHERE id=?", (attempts, next_at, item_id))
        await self.c.commit()

    async def outbox_count(self) -> int:
        cur = await self.c.execute("SELECT COUNT(*) FROM outbox")
        return (await cur.fetchone())[0]

    @property
    def c(self) -> aiosqlite.Connection:
        assert self.conn is not None, "DB is not opened"
        return self.conn

    # ── users ─────────────────────────────────────────────────────────────
    async def touch_user(self, tg_id: int, username: str | None, first_name: str | None,
                         last_name: str | None) -> tuple[dict, bool]:
        """Создаёт/обновляет пользователя. Возвращает (user, created)."""
        ts = _ts()
        row = await self.get_user(tg_id)
        if row is None:
            role = "admin" if tg_id == ADMIN_ID else "pending"
            await self.c.execute(
                "INSERT INTO users (tg_id, username, first_name, last_name, role, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (tg_id, username, first_name, last_name, role, ts, ts),
            )
            await self.c.commit()
            return await self.get_user(tg_id), True  # type: ignore[return-value]
        await self.c.execute(
            "UPDATE users SET username=?, first_name=?, last_name=?, updated_at=? WHERE tg_id=?",
            (username, first_name, last_name, ts, tg_id),
        )
        if tg_id == ADMIN_ID and row["role"] != "admin":
            await self.c.execute("UPDATE users SET role='admin' WHERE tg_id=?", (tg_id,))
        await self.c.commit()
        return await self.get_user(tg_id), False  # type: ignore[return-value]

    async def get_user(self, tg_id: int) -> dict | None:
        cur = await self.c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        user = dict(row)
        if tg_id == ADMIN_ID:
            user["role"] = "admin"
        return user

    async def set_role(self, tg_id: int, role: str) -> None:
        if tg_id == ADMIN_ID:
            return
        await self.c.execute("UPDATE users SET role=?, updated_at=? WHERE tg_id=?", (role, _ts(), tg_id))
        await self.c.commit()

    async def set_master_name(self, tg_id: int, name: str) -> None:
        await self.c.execute("UPDATE users SET master_name=?, updated_at=? WHERE tg_id=?", (name, _ts(), tg_id))
        await self.c.commit()

    async def list_users(self) -> list[dict]:
        cur = await self.c.execute(
            "SELECT * FROM users ORDER BY CASE role WHEN 'pending' THEN 0 WHEN 'admin' THEN 1"
            " WHEN 'master' THEN 2 ELSE 3 END, created_at"
        )
        return [dict(r) for r in await cur.fetchall()]

    # ── ciphers ───────────────────────────────────────────────────────────
    @staticmethod
    def _cipher(row: Any) -> dict:
        d = dict(row)
        d["short"] = short_code(d["code"])
        return d

    async def list_ciphers(self) -> list[dict]:
        cur = await self.c.execute("SELECT * FROM ciphers ORDER BY code")
        items = [self._cipher(r) for r in await cur.fetchall()]
        items.sort(key=lambda x: (x["short"].lower(), x["code"].lower()))
        return items

    async def add_ciphers(self, items: list[dict]) -> tuple[int, list[str]]:
        """items: [{code, building}] → (добавлено, пропущенные дубли)."""
        added, skipped = 0, []
        ts = _ts()
        for it in items:
            code = (it.get("code") or "").strip()
            if not code:
                continue
            building = (it.get("building") or "").strip()
            try:
                await self.c.execute(
                    "INSERT INTO ciphers (code, building, created_at) VALUES (?,?,?)", (code, building, ts)
                )
                added += 1
            except aiosqlite.IntegrityError:
                skipped.append(code)
        await self.c.commit()
        return added, skipped

    async def update_cipher(self, cid: int, code: str, building: str) -> None:
        await self.c.execute("UPDATE ciphers SET code=?, building=? WHERE id=?", (code.strip(), building.strip(), cid))
        await self.c.commit()

    async def delete_cipher(self, cid: int) -> None:
        await self.c.execute("DELETE FROM ciphers WHERE id=?", (cid,))
        await self.c.commit()

    # ── employees ─────────────────────────────────────────────────────────
    async def list_employees(self, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM employees" + ("" if include_deleted else " WHERE deleted=0")
        cur = await self.c.execute(sql)
        items = [dict(r) for r in await cur.fetchall()]
        items.sort(key=lambda e: norm_name(e["full_name"]))
        return items

    async def get_employee(self, eid: int) -> dict | None:
        cur = await self.c.execute("SELECT * FROM employees WHERE id=?", (eid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_employees(self, items: list[dict]) -> int:
        ts = _ts()
        n = 0
        for it in items:
            name = " ".join((it.get("full_name") or "").split())
            if not name:
                continue
            position = (it.get("position") or "").strip()
            status = it.get("status") if it.get("status") in STATUSES else "work"
            await self.c.execute(
                "INSERT INTO employees (full_name, position, status, created_at) VALUES (?,?,?,?)",
                (name, position, status, ts),
            )
            n += 1
        await self.c.commit()
        return n

    async def update_employee(self, eid: int, full_name: str, position: str) -> None:
        await self.c.execute(
            "UPDATE employees SET full_name=?, position=? WHERE id=?",
            (" ".join(full_name.split()), position.strip(), eid),
        )
        await self.c.commit()

    async def set_employee_status(self, eid: int, status: str, by: int) -> None:
        if status not in STATUSES:
            raise ValueError("bad status")
        await self.c.execute(
            "UPDATE employees SET status=?, status_changed_at=?, status_changed_by=? WHERE id=?",
            (status, _ts(), by, eid),
        )
        await self.c.commit()

    async def delete_employee(self, eid: int) -> None:
        await self.c.execute("UPDATE employees SET deleted=1 WHERE id=?", (eid,))
        await self.c.commit()

    # ── dispatches ────────────────────────────────────────────────────────
    @staticmethod
    def _dispatch(row: Any) -> dict:
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d

    async def save_dispatch(self, author_id: int, dispatch_id: int | None, data: dict, text: str) -> tuple[dict, bool]:
        """Создаёт или обновляет разнарядку. Возвращает (разнарядка, это_обновление)."""
        ts = _ts()
        date_s = data["date"]
        master = data["master"].strip()
        mnorm = norm_name(master)
        payload = json.dumps(data, ensure_ascii=False)
        is_update = False

        existing = None
        if dispatch_id:
            cur = await self.c.execute("SELECT * FROM dispatches WHERE id=? AND author_id=?", (dispatch_id, author_id))
            existing = await cur.fetchone()
        # та же дата + тот же мастер у этого автора — перезаписываем, а не плодим дубли
        cur = await self.c.execute(
            "SELECT * FROM dispatches WHERE author_id=? AND date=? AND master_norm=?", (author_id, date_s, mnorm)
        )
        same_slot = await cur.fetchone()

        if existing is not None:
            if same_slot is not None and same_slot["id"] != existing["id"]:
                await self.c.execute("DELETE FROM dispatches WHERE id=?", (same_slot["id"],))
            await self.c.execute(
                "UPDATE dispatches SET date=?, master_name=?, master_norm=?, data=?, text=?, updated_at=? WHERE id=?",
                (date_s, master, mnorm, payload, text, ts, existing["id"]),
            )
            new_id = existing["id"]
            is_update = True
        elif same_slot is not None:
            await self.c.execute(
                "UPDATE dispatches SET master_name=?, data=?, text=?, updated_at=? WHERE id=?",
                (master, payload, text, ts, same_slot["id"]),
            )
            new_id = same_slot["id"]
            is_update = True
        else:
            cur = await self.c.execute(
                "INSERT INTO dispatches (author_id, date, master_name, master_norm, data, text, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (author_id, date_s, master, mnorm, payload, text, ts, ts),
            )
            new_id = cur.lastrowid
        await self.c.commit()
        return await self.get_dispatch(new_id), is_update  # type: ignore[arg-type,return-value]

    async def get_dispatch(self, did: int) -> dict | None:
        cur = await self.c.execute("SELECT * FROM dispatches WHERE id=?", (did,))
        row = await cur.fetchone()
        return self._dispatch(row) if row else None

    async def list_my_dispatches(self, author_id: int, limit: int = 30) -> list[dict]:
        cur = await self.c.execute(
            "SELECT * FROM dispatches WHERE author_id=? ORDER BY date DESC, updated_at DESC LIMIT ?",
            (author_id, limit),
        )
        return [self._dispatch(r) for r in await cur.fetchall()]

    async def latest_dispatch(self, author_id: int) -> dict | None:
        cur = await self.c.execute(
            "SELECT * FROM dispatches WHERE author_id=? ORDER BY date DESC, updated_at DESC LIMIT 1", (author_id,)
        )
        row = await cur.fetchone()
        return self._dispatch(row) if row else None

    async def delete_dispatch(self, did: int, author_id: int | None = None) -> bool:
        if author_id is None:
            cur = await self.c.execute("DELETE FROM dispatches WHERE id=?", (did,))
        else:
            cur = await self.c.execute("DELETE FROM dispatches WHERE id=? AND author_id=?", (did, author_id))
        await self.c.commit()
        return cur.rowcount > 0

    async def dispatches_for_date(self, date_s: str) -> list[dict]:
        """Разнарядки на дату: по одной (последней) на каждого мастера.
        Порядок мастеров — стабильный (по дате регистрации автора)."""
        cur = await self.c.execute(
            "SELECT d.*, COALESCE(u.created_at, d.created_at) AS author_since FROM dispatches d"
            " LEFT JOIN users u ON u.tg_id = d.author_id WHERE d.date=?"
            " ORDER BY d.updated_at DESC",
            (date_s,),
        )
        rows = [self._dispatch(r) for r in await cur.fetchall()]
        seen: dict[str, dict] = {}
        for r in rows:
            seen.setdefault(r["master_norm"], r)
        result = list(seen.values())
        result.sort(key=lambda r: (r["author_since"], r["created_at"]))
        return result

    async def recent_master_names(self, days_back_from: str, limit_days: int = 14) -> list[str]:
        cur = await self.c.execute(
            "SELECT master_name, master_norm, MAX(date) AS last FROM dispatches"
            " WHERE date >= date(?, ?) AND date < ? GROUP BY master_norm ORDER BY last DESC",
            (days_back_from, f"-{limit_days} days", days_back_from),
        )
        return [r["master_name"] for r in await cur.fetchall()]

    # ── drafts ────────────────────────────────────────────────────────────
    async def get_draft(self, author_id: int) -> dict | None:
        cur = await self.c.execute("SELECT * FROM drafts WHERE author_id=?", (author_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return {"data": json.loads(row["data"]), "updated_at": row["updated_at"]}

    async def set_draft(self, author_id: int, data: dict) -> None:
        await self.c.execute(
            "INSERT INTO drafts (author_id, data, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(author_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (author_id, json.dumps(data, ensure_ascii=False), _ts()),
        )
        await self.c.commit()

    async def delete_draft(self, author_id: int) -> None:
        await self.c.execute("DELETE FROM drafts WHERE author_id=?", (author_id,))
        await self.c.commit()

    # ── helpers for the UI ────────────────────────────────────────────────
    async def suggestions(self, author_id: int) -> dict:
        """Подсказки из истории мастера: названия работ, единицы, техника."""
        cur = await self.c.execute(
            "SELECT data FROM dispatches WHERE author_id=? ORDER BY date DESC LIMIT 60", (author_id,)
        )
        works: dict[str, None] = {}
        units: dict[str, None] = {}
        tech: dict[str, None] = {}
        for (raw,) in await cur.fetchall():
            data = json.loads(raw)
            for obj in data.get("objects", []):
                for plan in obj.get("plans", []):
                    for t in plan.get("tech", []) or []:
                        tech.setdefault(t, None)
                    for w in plan.get("works", []):
                        if w.get("name"):
                            works.setdefault(w["name"].strip(), None)
                        if w.get("unit"):
                            units.setdefault(w["unit"].strip(), None)
        return {"works": list(works)[:300], "units": list(units), "tech": list(tech)}

    async def busy_for_date(self, date_s: str, exclude_author: int) -> dict[int, list[str]]:
        """Кто из сотрудников уже стоит в разнарядках других мастеров на эту дату."""
        busy: dict[int, list[str]] = {}
        for d in await self.dispatches_for_date(date_s):
            if d["author_id"] == exclude_author:
                continue
            for obj in d["data"].get("objects", []):
                for plan in obj.get("plans", []):
                    for p in plan.get("crew", []):
                        names = busy.setdefault(int(p["id"]), [])
                        label = f'{d["master_name"]} ({obj.get("short", "")})'
                        if label not in names:
                            names.append(label)
            for p in d["data"].get("dayoff") or []:
                names = busy.setdefault(int(p["id"]), [])
                label = f'{d["master_name"]} (выходной)'
                if label not in names:
                    names.append(label)
        return busy

    async def absent_ids_set_by(self, tg_id: int) -> list[int]:
        q = ",".join("?" * len(ABSENT_STATUSES))
        cur = await self.c.execute(
            f"SELECT id FROM employees WHERE deleted=0 AND status IN ({q}) AND status_changed_by=?",
            (*ABSENT_STATUSES, tg_id),
        )
        return [r[0] for r in await cur.fetchall()]


db = DB()
