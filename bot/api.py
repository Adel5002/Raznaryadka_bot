"""HTTP API для мини-аппа + раздача статики.

Каждый запрос к /api/* подписан Telegram (initData). Подпись проверяется
токеном бота, поэтому подделать пользователя нельзя.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from .config import ADMIN_ID, BOT_TOKEN, WEBAPP_DIR
from . import outbox
from .db import db
from .logs import REPORTS_DIR, rid_var, who
from .notify import (
    TelegramUnavailable,
    notify_admin_access_request,
    notify_admin_dispatch,
    notify_user_access,
    send_summary,
    send_text_summary,
    send_text_to_user,
)
from .utils import (
    ABSENT_STATUSES, STATUSES, day_totals, default_dispatch_date, norm_name, parse_date, short_code, today,
)

log = logging.getLogger(__name__)
access_log = logging.getLogger("access")
INIT_DATA_MAX_AGE = 24 * 3600

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


def ok(data: Any = None) -> web.Response:
    return web.json_response({"ok": True, **({"data": data} if data is not None else {})},
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


# ── журнал запросов ───────────────────────────────────────────────────────
@web.middleware
async def log_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Каждому запросу — короткий номер [rid] во всех строках лога; итог — строка в access.log."""
    rid = secrets.token_hex(3)
    token = rid_var.set(rid)
    t0 = time.monotonic()
    status = 500
    try:
        resp = await handler(request)
        status = resp.status
        resp.headers["X-Request-Id"] = rid
        return resp
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
        if request.path != "/health":  # сторож проверяет каждые 30 с — не засоряем
            user = request.get("user")
            ip = (request.headers.get("Fly-Client-IP")  # на Fly.io — настоящий адрес телефона
                  or request.headers.get("X-Forwarded-For", request.remote or "-").split(",")[0].strip())
            access_log.info("%s %s %s %s → %s %.0f мс", ip, f"uid={user['tg_id']}" if user else "uid=-",
                            request.method, request.path_qs[:200], status, (time.monotonic() - t0) * 1000)
        rid_var.reset(token)


# ── auth ──────────────────────────────────────────────────────────────────
@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        init_data = request.headers.get("X-Init-Data", "")
        if not init_data:
            raise ApiError("Откройте приложение через Telegram", 401)
        try:
            parsed = safe_parse_webapp_init_data(BOT_TOKEN, init_data)
        except ValueError:
            raise ApiError("Подпись Telegram не прошла проверку. Перезапустите мини-апп.", 401)
        auth_date = parsed.auth_date
        if auth_date.tzinfo is None:
            auth_date = auth_date.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - auth_date).total_seconds() > INIT_DATA_MAX_AGE:
            raise ApiError("Сессия устарела. Закройте и откройте мини-апп заново.", 401)
        tu = parsed.user
        if tu is None:
            raise ApiError("Нет данных пользователя", 401)
        user, created = await db.touch_user(tu.id, tu.username, tu.first_name, tu.last_name)
        if created and user["role"] == "pending":
            await notify_admin_access_request(user)
        request["user"] = user
        if user["role"] not in ("admin", "master") and request.path != "/api/bootstrap":
            raise ApiError("Нет доступа. Дождитесь подтверждения администратора.", 403)
        return await handler(request)
    except ApiError as e:
        if e.status >= 500:
            log.warning("Отказ %s %s %s → %s: %s", who(request.get("user")), request.method, request.path,
                        e.status, e.message)
        elif e.status != 401 or request.path != "/api/bootstrap":
            log.info("Отказ %s %s %s → %s: %s", who(request.get("user")), request.method, request.path,
                     e.status, e.message)
        return web.json_response({"ok": False, "error": e.message, **e.extra}, status=e.status,
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001
        rid = rid_var.get()
        log.exception("Ошибка сервера: %s %s %s", who(request.get("user")), request.method, request.path)
        return web.json_response(
            {"ok": False, "error": f"Ошибка сервера (код {rid}). Сообщите администратору этот код.", "rid": rid},
            status=500, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def me(request: web.Request) -> dict:
    return request["user"]


def require_admin(request: web.Request) -> None:
    if me(request)["tg_id"] != ADMIN_ID:
        raise ApiError("Только для администратора", 403)


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise ApiError("Некорректный запрос")
    if not isinstance(data, dict):
        raise ApiError("Некорректный запрос")
    return data


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _s(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


# ── views ─────────────────────────────────────────────────────────────────
def dispatch_brief(d: dict) -> dict:
    workers: set[int] = set()
    objs: list[str] = []
    for obj in d["data"].get("objects", []):
        if obj.get("short") and obj["short"] not in objs:
            objs.append(obj["short"])
        for plan in obj.get("plans", []):
            workers.update(int(p["id"]) for p in plan.get("crew", []))
    return {
        "id": d["id"], "date": d["date"], "master_name": d["master_name"], "author_id": d["author_id"],
        "objects": objs, "workers": len(workers), "itr_on_work": d["data"].get("itr_on_work", True),
        "updated_at": d["updated_at"], "created_at": d["created_at"], "text": d["text"],
    }


async def bootstrap(request: web.Request) -> web.Response:
    user = me(request)
    role = user["role"]
    base = {
        "me": {
            "id": user["tg_id"], "role": role, "is_admin": user["tg_id"] == ADMIN_ID,
            "master_name": user.get("master_name") or "", "first_name": user.get("first_name") or "",
        },
        "today": today().isoformat(),
        "default_date": default_dispatch_date().isoformat(),
        "statuses": STATUSES,
    }
    log.info("Мини-апп открыт: %s, роль %s · %s", who(user), role, _s(request.headers.get("X-Client"), 200) or "клиент ?")
    if role not in ("admin", "master"):
        return ok(base)
    mine = await db.list_my_dispatches(user["tg_id"])
    latest = mine[0] if mine else None
    base.update({
        "ciphers": await db.list_ciphers(),
        "employees": await db.list_employees(),
        "suggestions": await db.suggestions(user["tg_id"]),
        "draft": await db.get_draft(user["tg_id"]),
        "mine": [dispatch_brief(d) for d in mine],
        "latest": latest,
        "absent_by_me": await db.absent_ids_set_by(user["tg_id"]),
    })
    return ok(base)


async def list_mine(request: web.Request) -> web.Response:
    mine = await db.list_my_dispatches(me(request)["tg_id"])
    return ok([dispatch_brief(d) for d in mine])


async def get_dispatch(request: web.Request) -> web.Response:
    d = await db.get_dispatch(int(request.match_info["id"]))
    if d is None or (d["author_id"] != me(request)["tg_id"] and me(request)["tg_id"] != ADMIN_ID):
        raise ApiError("Разнарядка не найдена", 404)
    return ok(d)


async def _clean_dispatch(raw: dict) -> dict:
    """Проверяет и нормализует разнарядку из мини-аппа."""
    try:
        d = parse_date(str(raw.get("date", "")))
    except ValueError:
        raise ApiError("Укажите дату")
    if d < today():
        raise ApiError("Нельзя сделать разнарядку прошедшей датой")
    master = _s(raw.get("master"), 80)
    if not master:
        raise ApiError("Укажите, кто составляет разнарядку (мастер)")

    ciphers = {c["id"]: c for c in await db.list_ciphers()}
    employees = {e["id"]: e for e in await db.list_employees(include_deleted=True)}

    objects_in = raw.get("objects") or []
    if not isinstance(objects_in, list) or len(objects_in) > 60:
        raise ApiError("Некорректный список объектов")
    objects = []
    blocked: list[str] = []
    for oi, o in enumerate(objects_in, start=1):
        cid = o.get("cipher_id")
        cipher = ciphers.get(int(cid)) if isinstance(cid, int) or (isinstance(cid, str) and cid.isdigit()) else None
        if cipher:
            code, short, building = cipher["code"], cipher["short"], cipher["building"]
        else:
            code = _s(o.get("code") or o.get("short"), 120)
            short = _s(o.get("short"), 60) or short_code(code)
            building = _s(o.get("building"), 120)
            cid = None
        if not short:
            raise ApiError(f"Объект №{oi}: выберите объект")
        plans = []
        for pi, p in enumerate((o.get("plans") or [])[:60], start=1):
            works = []
            for w in (p.get("works") or [])[:100]:
                name = _s(w.get("name"), 400)
                if not name:
                    continue
                works.append({"name": name, "volume": _s(w.get("volume"), 20), "unit": _s(w.get("unit"), 20)})
            if not works:
                raise ApiError(f"{short}, план №{pi}: добавьте хотя бы одну работу")
            crew = []
            seen: set[int] = set()
            for pid in (p.get("crew") or [])[:200]:
                pid = int(pid.get("id") if isinstance(pid, dict) else pid)
                e = employees.get(pid)
                if e is None or e["deleted"] or pid in seen:
                    continue
                seen.add(pid)
                if e["status"] in ABSENT_STATUSES:
                    blocked.append(f'{e["full_name"]} ({STATUSES[e["status"]]})')
                crew.append({"id": pid, "name": e["full_name"]})
            tech = [_s(t, 40) for t in (p.get("tech") or [])[:20] if _s(t, 40)]
            plans.append({"works": works, "crew": crew, "tech": tech})
        if not plans:
            raise ApiError(f"Объект {short}: добавьте план")
        objects.append({"cipher_id": cid, "code": code, "short": short, "building": building, "plans": plans})
    if not objects:
        raise ApiError("Добавьте хотя бы один объект")
    if blocked:
        raise ApiError("Эти сотрудники не на работе и не могут стоять в плане: " + ", ".join(blocked))

    absent = []
    for a in (raw.get("absent") or [])[:300]:
        try:
            aid = int(a.get("id") if isinstance(a, dict) else a)
        except (TypeError, ValueError):
            continue
        e = employees.get(aid)
        if e and not e["deleted"] and e["status"] in ABSENT_STATUSES:
            absent.append({"id": aid, "name": e["full_name"], "status": e["status"]})

    # Выходной — только на дату этой разнарядки; в штатке статус не меняется
    in_crew = {p["id"] for o in objects for pl in o["plans"] for p in pl["crew"]}
    dayoff, seen_off = [], set()
    for a in (raw.get("dayoff") or [])[:300]:
        try:
            aid = int(a.get("id") if isinstance(a, dict) else a)
        except (TypeError, ValueError):
            continue
        e = employees.get(aid)
        if e is None or e["deleted"] or aid in seen_off or e["status"] in ABSENT_STATUSES:
            continue
        if aid in in_crew:
            raise ApiError(f'{e["full_name"]} отмечен(а) выходным и не может стоять в плане')
        seen_off.add(aid)
        dayoff.append({"id": aid, "name": e["full_name"]})

    return {
        "date": d.isoformat(),
        "master": master,
        "itr_on_work": bool(raw.get("itr_on_work", True)),
        "objects": objects,
        "absent": absent,
        "dayoff": dayoff,
        "note": str(raw.get("note") or "").strip()[:2000],
    }


# Повтор того же запроса (телефон не дождался ответа и отправил ещё раз) не должен
# создавать вторую разнарядку и второй пуш. Помним последние ответы по req_id.
_recent_saves: OrderedDict[tuple[int, str], dict] = OrderedDict()
_RECENT_MAX = 500


async def save_dispatch(request: web.Request) -> web.Response:
    user = me(request)
    payload = await body(request)
    req_id = _s(payload.get("req_id"), 64)
    key = (user["tg_id"], req_id)
    if req_id and key in _recent_saves:
        log.info("Повтор отправки разнарядки (req_id %s) от %s — дубль не создаю, #%s",
                 req_id, who(user), _recent_saves[key]["dispatch"]["id"])
        return ok(_recent_saves[key])
    data = await _clean_dispatch(payload.get("data") or {})
    text = str(payload.get("text") or "").strip()[:12000]
    if not text:
        raise ApiError("Пустой текст разнарядки")
    did = payload.get("id")
    dispatch, is_update = await db.save_dispatch(user["tg_id"], int(did) if did else None, data, text)
    await db.set_master_name(user["tg_id"], data["master"])
    _close_draft_mark(user["tg_id"], payload.get("draft_ts"))
    await db.delete_draft(user["tg_id"])
    await notify_admin_dispatch(dispatch, is_update)
    brief = dispatch_brief(dispatch)
    log.info("Разнарядка #%s %s: %s, на %s, мастер «%s», объекты %s, людей %d, ИТР %s",
             dispatch["id"], "обновлена" if is_update else "создана", who(user), data["date"], data["master"],
             "/".join(brief["objects"]) or "—", brief["workers"], "выходит" if data["itr_on_work"] else "нет")
    result = {"dispatch": dispatch, "is_update": is_update}
    if req_id:
        _recent_saves[key] = result
        while len(_recent_saves) > _RECENT_MAX:
            _recent_saves.popitem(last=False)
    return ok(result)


async def delete_dispatch(request: web.Request) -> web.Response:
    user = me(request)
    did = int(request.match_info["id"])
    author = None if user["tg_id"] == ADMIN_ID else user["tg_id"]
    if not await db.delete_dispatch(did, author):
        raise ApiError("Разнарядка не найдена", 404)
    log.info("Разнарядка #%s удалена: %s", did, who(user))
    return ok()


async def dispatch_to_chat(request: web.Request) -> web.Response:
    d = await db.get_dispatch(int(request.match_info["id"]))
    if d is None or (d["author_id"] != me(request)["tg_id"] and me(request)["tg_id"] != ADMIN_ID):
        raise ApiError("Разнарядка не найдена", 404)
    await send_text_to_user(me(request)["tg_id"], d["text"])
    log.info("Текст разнарядки #%s отправлен в чат %s", d["id"], who(me(request)))
    return ok()


async def busy(request: web.Request) -> web.Response:
    date_s = request.query.get("date", "")
    try:
        parse_date(date_s)
    except ValueError:
        raise ApiError("Некорректная дата")
    return ok(await db.busy_for_date(date_s, me(request)["tg_id"]))


# Когда черновик последний раз «закрыт» (отправлен или удалён) — по часам телефона.
# Запоздавшее сохранение, отправленное раньше этого момента, черновик не воскрешает.
_draft_closed: dict[int, float] = {}


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _close_draft_mark(tg_id: int, ts: Any) -> None:
    t = _num(ts)
    if t is not None:
        _draft_closed[tg_id] = max(t, _draft_closed.get(tg_id, 0))


async def put_draft(request: web.Request) -> web.Response:
    payload = await body(request)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ApiError("Некорректный черновик")
    if len(json.dumps(data, ensure_ascii=False)) > 300_000:
        raise ApiError("Черновик слишком большой")
    uid = me(request)["tg_id"]
    ts = _num(data.get("_ts"))
    if ts is not None:
        # запросы могут прийти не по порядку (повторы после обрыва) — старое не затирает новое
        if ts <= _draft_closed.get(uid, 0):
            return ok({"stale": True})
        cur = await db.get_draft(uid)
        cur_ts = _num(cur["data"].get("_ts")) if cur else None
        if cur_ts is not None and cur_ts > ts:
            return ok({"stale": True})
    await db.set_draft(uid, data)
    return ok()


async def delete_draft(request: web.Request) -> web.Response:
    uid = me(request)["tg_id"]
    try:
        _close_draft_mark(uid, float(request.query.get("ts", "")))
    except ValueError:
        pass
    await db.delete_draft(uid)
    return ok()


def _trail(raw: Any, limit: int) -> list[str]:
    return [str(x)[:300] for x in raw[-limit:]] if isinstance(raw, list) else []


async def client_error(request: web.Request) -> web.Response:
    """Ошибки JS из мини-аппа — в лог бота вместе с последними действиями на телефоне."""
    payload = await body(request)
    trail = _trail(payload.get("trail"), 25)
    log.warning("Ошибка в мини-аппе: %s, экран %s, %s\n  %s\n  Последние действия:\n    %s",
                who(me(request)), _s(payload.get("view"), 40), _s(json.dumps(payload.get("env"), ensure_ascii=False), 300),
                str(payload.get("msg") or "")[:1500].replace("\n", "\n  "), "\n    ".join(trail) or "—")
    return ok()


async def report_problem(request: web.Request) -> web.Response:
    """«Сообщить о проблеме» из мини-аппа: сохраняем с журналом действий и зовём админа."""
    user = me(request)
    payload = await body(request)
    rep_id = "".join(ch for ch in _s(payload.get("id"), 24) if ch.isalnum()) or secrets.token_hex(4)
    if any(REPORTS_DIR.glob(f"*-{rep_id}.json")):
        return ok({"id": rep_id})  # повтор того же отчёта
    text = str(payload.get("text") or "").strip()[:3000]
    now = datetime.now()
    rec = {
        "id": rep_id, "at": now.isoformat(timespec="seconds"), "who": who(user), "user_id": user["tg_id"],
        "role": user["role"], "text": text, "view": _s(payload.get("view"), 40), "env": payload.get("env"),
        "trail": _trail(payload.get("trail"), 200),
        "draft": payload.get("draft") if len(json.dumps(payload.get("draft"), ensure_ascii=False)) < 200_000 else None,
        "server_rid": rid_var.get(),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{now:%Y%m%d-%H%M%S}-{user['tg_id']}-{rep_id}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    log.warning("Сообщение о проблеме %s от %s (экран %s): %s", rep_id, who(user), rec["view"], text[:500] or "—")
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📦 Прислать логи", callback_data="logs:send")]])
    await outbox.send(ADMIN_ID, f"🐞 <b>Сообщение о проблеме</b> от {html_escape(who(user))}\n"
                                f"«{html_escape(text[:1500]) or '—'}»\n\n"
                                f"Нажмите кнопку — пришлю архив логов, его можно переслать разработчику.",
                      reply_markup=kb)
    return ok({"id": rep_id})


async def admin_logs_send(request: web.Request) -> web.Response:
    require_admin(request)
    from .handlers import send_logs  # здесь, чтобы не было циклического импорта
    try:
        await send_logs(ADMIN_ID)
    except TelegramUnavailable:
        raise ApiError("Сейчас нет связи с Telegram — архив не отправлен. Попробуйте через минуту "
                       "или запустите collect_logs.bat на компьютере с ботом.", 503)
    return ok()


async def list_employees(request: web.Request) -> web.Response:
    return ok(await db.list_employees())


async def set_employee_status(request: web.Request) -> web.Response:
    eid = int(request.match_info["id"])
    status = (await body(request)).get("status")
    if status not in STATUSES:
        raise ApiError("Неизвестный статус")
    e = await db.get_employee(eid)
    if e is None or e["deleted"]:
        raise ApiError("Сотрудник не найден", 404)
    await db.set_employee_status(eid, status, me(request)["tg_id"])
    log.info("Статус: %s (#%s) %s → %s, поставил %s", e["full_name"], eid, e["status"], status, who(me(request)))
    return ok(await db.get_employee(eid))


# ── admin ─────────────────────────────────────────────────────────────────
async def admin_add_ciphers(request: web.Request) -> web.Response:
    require_admin(request)
    items = (await body(request)).get("items") or []
    clean = [{"code": _s(i.get("code"), 120), "building": _s(i.get("building"), 120)} for i in items[:500]]
    added, skipped = await db.add_ciphers([c for c in clean if c["code"]])
    log.info("Шифры: добавлено %s, уже были %d", added, len(skipped))
    return ok({"added": added, "skipped": skipped, "ciphers": await db.list_ciphers()})


async def admin_update_cipher(request: web.Request) -> web.Response:
    require_admin(request)
    payload = await body(request)
    code = _s(payload.get("code"), 120)
    if not code:
        raise ApiError("Шифр не может быть пустым")
    try:
        await db.update_cipher(int(request.match_info["id"]), code, _s(payload.get("building"), 120))
    except Exception:  # noqa: BLE001
        raise ApiError("Такой шифр уже есть")
    log.info("Шифр #%s изменён: %s", request.match_info["id"], code)
    return ok(await db.list_ciphers())


async def admin_delete_cipher(request: web.Request) -> web.Response:
    require_admin(request)
    await db.delete_cipher(int(request.match_info["id"]))
    log.info("Шифр #%s удалён", request.match_info["id"])
    return ok(await db.list_ciphers())


async def admin_add_employees(request: web.Request) -> web.Response:
    require_admin(request)
    items = (await body(request)).get("items") or []
    clean = [{"full_name": _s(i.get("full_name"), 120), "position": _s(i.get("position"), 80)} for i in items[:500]]
    n = await db.add_employees([c for c in clean if c["full_name"]])
    log.info("Штатка: добавлено %s чел. (%s)", n, ", ".join(c["full_name"] for c in clean[:10]))
    return ok({"added": n, "employees": await db.list_employees()})


async def admin_update_employee(request: web.Request) -> web.Response:
    require_admin(request)
    eid = int(request.match_info["id"])
    payload = await body(request)
    name = _s(payload.get("full_name"), 120)
    if not name:
        raise ApiError("ФИО не может быть пустым")
    await db.update_employee(eid, name, _s(payload.get("position"), 80))
    log.info("Сотрудник #%s изменён: %s, %s", eid, name, _s(payload.get("position"), 80))
    status = payload.get("status")
    if status in STATUSES:
        e = await db.get_employee(eid)
        if e and e["status"] != status:
            await db.set_employee_status(eid, status, me(request)["tg_id"])
    return ok(await db.list_employees())


async def admin_delete_employee(request: web.Request) -> web.Response:
    require_admin(request)
    await db.delete_employee(int(request.match_info["id"]))
    log.info("Сотрудник #%s удалён из штатки", request.match_info["id"])
    return ok(await db.list_employees())


async def admin_users(request: web.Request) -> web.Response:
    require_admin(request)
    return ok(await db.list_users())


async def admin_set_role(request: web.Request) -> web.Response:
    require_admin(request)
    uid = int(request.match_info["id"])
    role = (await body(request)).get("role")
    if role not in ("master", "blocked") or uid == ADMIN_ID:
        raise ApiError("Нельзя")
    before = await db.get_user(uid)
    await db.set_role(uid, role)
    log.info("Доступ: %s → %s (из мини-аппа)", who(before) if before else uid, role)
    if before and before["role"] != role:
        await notify_user_access(uid, role == "master")
    return ok(await db.list_users())


async def admin_summary(request: web.Request) -> web.Response:
    require_admin(request)
    date_s = request.query.get("date", "")
    try:
        parse_date(date_s)
    except ValueError:
        raise ApiError("Некорректная дата")
    items = await db.dispatches_for_date(date_s)
    submitted = {d["master_norm"] for d in items}
    missing = [n for n in await db.recent_master_names(date_s) if norm_name(n) not in submitted]
    t = day_totals(items, await db.list_employees())
    return ok({
        "date": date_s,
        "dispatches": [dispatch_brief(d) for d in items],
        "missing": missing,
        "workers": t["workers"],
        "drivers": t["drivers"],
        "itr": t["itr"],
    })


async def admin_summary_send(request: web.Request) -> web.Response:
    require_admin(request)
    date_s = str((await body(request)).get("date", ""))
    try:
        parse_date(date_s)
    except ValueError:
        raise ApiError("Некорректная дата")
    try:
        sent = await send_summary(ADMIN_ID, date_s)
    except TelegramUnavailable:
        raise ApiError("Сейчас нет связи с Telegram — Excel не отправлен. Попробуйте через минуту.", 503)
    if not sent:
        raise ApiError("На эту дату нет разнарядок")
    return ok()


async def admin_summary_text(request: web.Request) -> web.Response:
    require_admin(request)
    date_s = str((await body(request)).get("date", ""))
    try:
        parse_date(date_s)
    except ValueError:
        raise ApiError("Некорректная дата")
    if not await send_text_summary(ADMIN_ID, date_s):
        raise ApiError("На эту дату нет разнарядок")
    return ok()


# ── static ────────────────────────────────────────────────────────────────
NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


async def index(_: web.Request) -> web.Response:
    html = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    version = str(int(max((WEBAPP_DIR / f).stat().st_mtime for f in ("app.js", "style.css"))))
    return web.Response(text=html.replace("{{V}}", version), content_type="text/html", headers=NO_CACHE)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application(middlewares=[log_middleware, auth_middleware], client_max_size=2 * 1024 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", health)
    r.add_static("/static/", WEBAPP_DIR, append_version=False)

    r.add_get("/api/bootstrap", bootstrap)
    r.add_get("/api/dispatches", list_mine)
    r.add_post("/api/dispatches", save_dispatch)
    r.add_get("/api/dispatches/{id:\\d+}", get_dispatch)
    r.add_delete("/api/dispatches/{id:\\d+}", delete_dispatch)
    r.add_post("/api/dispatches/{id:\\d+}/to_chat", dispatch_to_chat)
    r.add_get("/api/busy", busy)
    r.add_put("/api/draft", put_draft)
    r.add_delete("/api/draft", delete_draft)
    r.add_get("/api/employees", list_employees)
    r.add_post("/api/employees/{id:\\d+}/status", set_employee_status)
    r.add_post("/api/client_error", client_error)
    r.add_post("/api/report", report_problem)

    r.add_post("/api/admin/ciphers", admin_add_ciphers)
    r.add_put("/api/admin/ciphers/{id:\\d+}", admin_update_cipher)
    r.add_delete("/api/admin/ciphers/{id:\\d+}", admin_delete_cipher)
    r.add_post("/api/admin/employees", admin_add_employees)
    r.add_put("/api/admin/employees/{id:\\d+}", admin_update_employee)
    r.add_delete("/api/admin/employees/{id:\\d+}", admin_delete_employee)
    r.add_get("/api/admin/users", admin_users)
    r.add_post("/api/admin/users/{id:\\d+}/role", admin_set_role)
    r.add_get("/api/admin/summary", admin_summary)
    r.add_post("/api/admin/summary/send", admin_summary_send)
    r.add_post("/api/admin/summary/text", admin_summary_text)
    r.add_post("/api/admin/logs/send", admin_logs_send)
    return app
