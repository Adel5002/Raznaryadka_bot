'use strict';
/* Мини-апп «Разнарядка». Чистый JS без сборки. */

const tg = window.Telegram && window.Telegram.WebApp;
const $app = document.getElementById('app');
const $tabs = document.getElementById('tabs');
const $sheetRoot = document.getElementById('sheet-root');

const SEP = '━━━━━━━━━━━━━';
const OBJ_SEP = '━━━━━━━━━━━━━━━━'; // между объектами — чуть длиннее (как OBJ_SEP в bot/textfmt.py)
const WD = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
const ABSENT = ['bs', 'otpusk', 'mezhvahta'];
const ABSENT_TITLE = { bs: 'БС', otpusk: 'ОТПУСК', mezhvahta: 'МЕЖВАХТА' };
const UNITS = ['м', 'шт', 'м²', 'м³', 'п.м.', 'т', 'кг', 'компл.', 'л', 'слои'];
const TECH = ['JCB', 'Автокран', 'Самосвал', 'Манипулятор', 'Экскаватор', 'Погрузчик', 'Бетононасос', 'Миксер'];
const NOTE_PRESET = 'возможна ротация людей между объектами для оптимизации работ';

const S = {
  boot: null,
  emp: new Map(),
  tab: 'disp',
  view: 'home',          // home | editor | dispatch
  draft: null,
  busy: {},
  dispatch: null,        // открытая разнарядка (полная)
  justSent: false,
  sheet: null,
  staffFilter: 'all',
  staffQuery: '',
  cipherQuery: '',
  summaryDate: null,
  summary: null,
  users: null,
  saveState: '',         // saving | saved | local
  undo: null,            // копия разнарядки до «Очистить» — для «Вернуть как было»
  offerClear: false,     // после смены даты предложить заполнить заново
  submitReq: null,       // {sig, id} — повтор той же отправки не создаёт дубль
  fatal: null,           // 'moved' | 'auth' — нужно переоткрыть приложение
};
window.__rzBooted = true;
const APP_V = (() => { try { return new URL(document.currentScript.src).searchParams.get('v') || '?'; } catch (_) { return '?'; } })();

/* Журнал действий на телефоне (последние 150 строк). Уходит вместе с ошибкой
   и с «Сообщить о проблеме» — по нему видно, что человек нажимал и что ответил сервер. */
const TRAIL = [];
function trail(kind, msg) {
  const t = new Date();
  const p2 = (n) => String(n).padStart(2, '0');
  TRAIL.push(`${p2(t.getHours())}:${p2(t.getMinutes())}:${p2(t.getSeconds())}.${String(t.getMilliseconds()).padStart(3, '0')} ${kind} ${String(msg).slice(0, 240)}`);
  if (TRAIL.length > 150) TRAIL.shift();
}
function clientEnv() {
  return {
    app: APP_V, tg: tg ? tg.version : null, platform: tg ? tg.platform : null,
    screen: `${window.innerWidth}x${window.innerHeight}`, online: navigator.onLine, ua: navigator.userAgent.slice(0, 200),
  };
}
const CLIENT_HDR = `app=${APP_V}; tg=${tg ? tg.version : '-'}; ${tg ? tg.platform : '-'}`;

/* ───────────── утилиты ───────────── */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const uid = () => Math.random().toString(36).slice(2, 10);
const norm = (s) => String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();
const byName = (a, b) => a.full_name.localeCompare(b.full_name, 'ru');
function fmtDate(iso) { if (!iso) return ''; const [y, m, d] = iso.split('-'); return `${d}.${m}.${y}`; }
function wd(iso) { const [y, m, d] = iso.split('-').map(Number); return WD[new Date(y, m - 1, d).getDay()]; }
function addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  const t = new Date(y, m - 1, d + n);
  return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
}
function nextWorkday(iso) { let d = addDays(iso, 1); if (wd(d) === 'вс') d = addDays(d, 1); return d; }
function plural(n, one, few, many) {
  const a = n % 10, b = n % 100;
  if (a === 1 && b !== 11) return one;
  if (a >= 2 && a <= 4 && !(b >= 12 && b <= 14)) return few;
  return many;
}
function shortCode(code) {
  const c = String(code || '').trim();
  const p = c.split('.');
  return p.length >= 3 && p[2].trim() ? p[2].trim() : c;
}
function getPath(obj, path) { return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj); }
function setPath(obj, path, val) {
  const ks = path.split('.'); const last = ks.pop();
  const t = ks.reduce((o, k) => o[k], obj); t[last] = val;
}
const isAbsent = (e) => e && ABSENT.includes(e.status);
const statusLabel = (st) => (S.boot && S.boot.statuses[st]) || st;
const empName = (id, fallback) => (S.emp.get(id) || {}).full_name || fallback || '—';
const today = () => S.boot.today;
const isAdmin = () => S.boot && S.boot.me.is_admin;

function toast(msg, ms = 1900) {
  if (S.fatal) return;
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms);
}
function haptic(kind) {
  try {
    if (!tg || !tg.HapticFeedback) return;
    if (kind === 'light') tg.HapticFeedback.impactOccurred('light');
    else if (kind === 'select') tg.HapticFeedback.selectionChanged();
    else tg.HapticFeedback.notificationOccurred(kind);
  } catch (_) { /* старый клиент */ }
}
const tgv = (v) => !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast(v));
function confirmBox(msg) {
  return new Promise((res) => { if (tgv('6.2')) tg.showConfirm(msg.slice(0, 4000), (ok) => res(!!ok)); else res(window.confirm(msg)); });
}
function alertBox(msg) {
  trail('alert', msg.replace(/\n+/g, ' / '));
  if (S.fatal) return Promise.resolve();
  return new Promise((res) => {
    try { if (tgv('6.2')) { tg.showAlert(msg.slice(0, 4000), () => res()); return; } } catch (_) { /* попап уже открыт */ }
    window.alert(msg); res();
  });
}

/* ───────────── связь с сервером ─────────────
   Таймаут, повторы при обрыве связи и распознавание «бот перезапустился»:
   бесплатный ngrok после перезапуска даёт новый адрес, а старый отвечает 404
   своей страницей вместо нашего JSON — тогда просим переоткрыть приложение. */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const RETRY_DELAYS = [800, 2000, 4000];
const TRANSIENT = new Set(['network', 'timeout', 'gateway']);

class ApiError extends Error {
  constructor(message, status = 0, kind = 'api') { super(message); this.status = status; this.kind = kind; }
}

async function apiOnce(method, url, body, { timeout = 20000, keepalive = false } = {}) {
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeout) : null;
  const t0 = Date.now(); const path = url.split('?')[0];
  let res;
  try {
    res = await fetch(url, {
      method,
      cache: 'no-store',
      signal: ctrl ? ctrl.signal : undefined,
      ...(keepalive ? { keepalive: true } : {}),
      // ngrok-skip-browser-warning — чтобы бесплатный ngrok не подсовывал свою страницу-предупреждение вместо ответа API
      headers: { 'Content-Type': 'application/json', 'X-Init-Data': (tg && tg.initData) || '', 'ngrok-skip-browser-warning': '1', 'X-Client': CLIENT_HDR },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    trail('api', `${method} ${path} ✗ ${e && e.name === 'AbortError' ? 'таймаут' : 'нет связи'} ${Date.now() - t0}мс`);
    if (e && e.name === 'AbortError') throw new ApiError('Сервер долго не отвечает. Проверьте интернет и повторите.', 0, 'timeout');
    throw new ApiError(navigator.onLine === false ? 'Нет интернета. Как только связь появится — повторите.' : 'Нет связи с сервером. Проверьте интернет и повторите.', 0, 'network');
  } finally { if (timer) clearTimeout(timer); }
  const rid = res.headers.get('x-request-id') || '';
  const ngrokErr = res.headers.get('ngrok-error-code') || '';
  trail('api', `${method} ${path} → ${res.status} ${Date.now() - t0}мс${rid ? ' rid=' + rid : ''}${ngrokErr ? ' ' + ngrokErr : ''}`);
  let j = null;
  if ((res.headers.get('content-type') || '').includes('application/json')) { try { j = await res.json(); } catch (_) { j = null; } }
  if (j && typeof j === 'object' && 'ok' in j) {
    if (j.ok) return j.data;
    throw new ApiError(j.error || 'Ошибка', res.status, res.status === 401 ? 'auth' : 'api');
  }
  // Ответил не наш сервер (ngrok или прокси)
  if (res.status === 404 || ngrokErr === 'ERR_NGROK_3200') {
    throw new ApiError('Бот перезапускался — откройте приложение заново.', res.status, 'moved');
  }
  throw new ApiError('Сервер перезапускается — подождите несколько секунд и повторите.', res.status, 'gateway');
}

// opt.retry: повторять ли при обрыве. GET/PUT — всегда; POST — только там, где повтор безопасен.
async function api(method, url, body, opt = {}) {
  const retry = opt.retry ?? (method === 'GET' || method === 'PUT');
  for (let attempt = 0; ; attempt++) {
    try {
      return await apiOnce(method, url, body, opt);
    } catch (e) {
      if (e.kind === 'moved' || e.kind === 'auth') { showReopen(e); e.shown = true; throw e; }
      if (!retry || !TRANSIENT.has(e.kind) || attempt >= RETRY_DELAYS.length) throw e;
      await sleep(RETRY_DELAYS[attempt]);
    }
  }
}

function showReopen(err) {
  if (S.fatal) return;
  if (S.draft) saveLocalNow();
  S.fatal = err.kind;
  closeSheetQuiet();
  const box = document.createElement('div');
  box.className = 'fatal';
  box.innerHTML = `<div class="fatal-box"><div class="big-emoji">🔄</div>
    <h2>${err.kind === 'auth' ? 'Сессия устарела' : 'Бот перезапускался'}</h2>
    <p>${err.kind === 'auth' ? esc(err.message) : 'Адрес приложения обновился. Закройте его и откройте заново кнопкой «Разнарядка» в чате с ботом.'}</p>
    ${S.draft ? '<p class="hint">Черновик сохранён — продолжите с того же места.</p>' : ''}
    <button class="btn btn-primary wide" data-act="close-app">Закрыть приложение</button></div>`;
  document.body.appendChild(box);
  haptic('warning');
}

/* Ошибки, которых никто не ждал: короткое сообщение вместо «ничего не происходит» + запись в лог бота */
let errReports = 0; let lastErrToast = 0;
function reportError(e) {
  if (!e || e.shown || S.fatal) return;
  trail('error', (e && e.message) || e);
  if (e instanceof ApiError) { toast(e.message, 3500); return; }
  console.error(e);
  const now = Date.now();
  if (now - lastErrToast > 5000) { lastErrToast = now; toast('⚠️ Сбой в приложении. Если что-то не так — закройте и откройте заново, черновик сохранён.', 4500); }
  if (errReports++ < 5 && tg && tg.initData) {
    apiOnce('POST', '/api/client_error', {
      msg: String((e && (e.stack || e.message)) || e), view: `${S.tab}/${S.view}`, env: clientEnv(), trail: TRAIL.slice(-25),
    }).catch(() => {});
  }
}
window.addEventListener('error', (ev) => reportError(ev.error || new Error(ev.message)));
window.addEventListener('unhandledrejection', (ev) => reportError(ev.reason));

/* ───────────── копия черновика на телефоне ─────────────
   DeviceStorage (Telegram 9.0+) не зависит от адреса приложения — переживает смену адреса ngrok.
   localStorage — запасной вариант для старых клиентов. */
const devStore = () => (tgv('9.0') && tg.DeviceStorage ? tg.DeviceStorage : null);
const localKey = () => `rz_draft_${S.boot ? S.boot.me.id : 0}`;
function devCall(fn) {
  return new Promise((res) => {
    const t = setTimeout(() => res(null), 1500);
    try { fn((err, val) => { clearTimeout(t); res(err ? null : val); }); } catch (_) { clearTimeout(t); res(null); }
  });
}
function writeLocal(rec) {
  const s = rec ? JSON.stringify(rec) : null;
  try { if (s) localStorage.setItem(localKey(), s); else localStorage.removeItem(localKey()); } catch (_) { /* приватный режим */ }
  const ds = devStore();
  if (ds) devCall((cb) => (s ? ds.setItem(localKey(), s, cb) : ds.removeItem(localKey(), cb)));
}
async function readLocal() {
  const parse = (s) => { try { return s ? JSON.parse(s) : null; } catch (_) { return null; } };
  let a = null;
  try { a = parse(localStorage.getItem(localKey())); } catch (_) { /* нет */ }
  const ds = devStore();
  const b = ds ? parse(await devCall((cb) => ds.getItem(localKey(), cb))) : null;
  if (a && b) return (b.ts || 0) > (a.ts || 0) ? b : a;
  return a || b;
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext && window.ClipboardItem && navigator.clipboard.write) {
      const html = `<pre>${textHtml(text)}</pre>`;
      await navigator.clipboard.write([new ClipboardItem({
        'text/plain': new Blob([text], { type: 'text/plain' }),
        'text/html': new Blob([html], { type: 'text/html' }),
      })]);
      return true;
    }
  } catch (_) { /* ниже — только текст */ }
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch (_) { /* fallback */ }
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
  document.body.appendChild(ta); ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
  let ok = false; try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
  ta.remove(); return ok;
}

function indexEmployees() {
  S.boot.employees.sort(byName);
  S.emp = new Map(S.boot.employees.map((e) => [e.id, e]));
}
function updateEmployee(e) {
  const i = S.boot.employees.findIndex((x) => x.id === e.id);
  if (i >= 0) S.boot.employees[i] = e; else S.boot.employees.push(e);
  indexEmployees();
}

/* ───────────── разнарядка: модель и текст ───────────── */
const emptyWork = () => ({ k: uid(), name: '', volume: '', unit: '' });
const emptyPlan = () => ({ k: uid(), works: [emptyWork()], crew: [], tech: [] });
const emptyObject = () => ({ k: uid(), cipher_id: null, code: '', short: '', building: '', plans: [emptyPlan()] });

function draftFrom(data, { id = null, date = null } = {}) {
  const removed = [];
  const ciphers = new Map(S.boot.ciphers.map((c) => [c.id, c]));
  const objects = (data.objects || []).map((o) => {
    const c = o.cipher_id != null ? ciphers.get(Number(o.cipher_id)) : null;
    return {
      k: uid(),
      cipher_id: c ? c.id : null,
      code: c ? c.code : (o.code || ''),
      short: c ? c.short : (o.short || ''),
      building: c ? c.building : (o.building || ''),
      plans: (o.plans || []).map((p) => ({
        k: uid(),
        works: (p.works || []).map((w) => ({ k: uid(), name: w.name || '', volume: w.volume || '', unit: w.unit || '' })),
        tech: [...(p.tech || [])],
        crew: (p.crew || []).map((c2) => (typeof c2 === 'object' ? Number(c2.id) : Number(c2))).filter((pid, _i, arr) => {
          const e = S.emp.get(pid);
          if (!e) { removed.push('удалённый сотрудник'); return false; }
          if (isAbsent(e)) { removed.push(`${e.full_name} (${statusLabel(e.status)})`); return false; }
          return arr.indexOf(pid) === _i;
        }),
      })),
    };
  });
  objects.forEach((o) => { if (!o.plans.length) o.plans.push(emptyPlan()); o.plans.forEach((p) => { if (!p.works.length) p.works.push(emptyWork()); }); });
  const absentIds = new Set([...(data.absent || []).map((a) => Number(a.id ?? a)), ...(S.boot.absent_by_me || [])]);
  const absent = [...absentIds].filter((aid) => isAbsent(S.emp.get(aid))).map((aid) => ({ id: aid }));
  let d = date || data.date || S.boot.default_date;
  if (d < today()) d = S.boot.default_date;
  const draft = {
    v: 1, id, date: d,
    // дата отправленной разнарядки, которую правим: если дату сменить — она «переедет»
    orig_date: id ? (data.orig_date || date || data.date) : null,
    master: data.master || S.boot.me.master_name || '',
    itr_on_work: data.itr_on_work !== false,
    objects: objects.length ? objects : [emptyObject()],
    absent,
    note: data.note || '',
  };
  return { draft, removed };
}

function newDraft() {
  const absent = (S.boot.absent_by_me || []).filter((id) => isAbsent(S.emp.get(id))).map((id) => ({ id }));
  return { v: 1, id: null, orig_date: null, date: S.boot.default_date, master: S.boot.me.master_name || '', itr_on_work: true, objects: [emptyObject()], absent, note: '' };
}
function hasContent(d) {
  return !!(d && (d.note.trim() || d.objects.some((o) => o.short || o.plans.some((p) => p.crew.length || p.tech.length || p.works.some((w) => w.name.trim())))));
}

function crewIds(d) {
  const set = new Set();
  d.objects.forEach((o) => o.plans.forEach((p) => p.crew.forEach((id) => set.add(id))));
  return set;
}
function absentShown(d) { return d.absent.filter((a) => isAbsent(S.emp.get(a.id))); }

function workLine(w) {
  let s = '•' + w.name.trim();
  const v = String(w.volume || '').trim();
  if (v) s += ` - ${v}${String(w.unit || '').trim()}`;
  return s;
}
function totalsLine(d) {
  const n = crewIds(d).size;
  let s = `${n} ${plural(n, 'монтажник', 'монтажника', 'монтажников')}`;
  if (d.itr_on_work) s += `, 1 ИТР (${d.master.trim() || '—'})`;
  return s;
}
function textHtml(text) {
  return String(text).split('\n').map((l) => (l.startsWith('Объект:') ? `<b>${esc(l)}</b>` : esc(l))).join('\n');
}
function buildText(d) {
  const L = ['РАЗНАРЯДКА:', `Дата: ${fmtDate(d.date)}`, `Мастер : ${d.master.trim()}`, SEP];
  d.objects.forEach((o, oi) => {
    if (oi) L.push(OBJ_SEP);
    L.push(`Объект: ${o.short || '—'}`);
    o.plans.forEach((p, pi) => {
      if (pi) L.push('');
      L.push('План:');
      p.works.filter((w) => w.name.trim()).forEach((w) => L.push(workLine(w)));
      if (p.tech.length) L.push(`Техника: ${p.tech.join(', ')}`);
      if (p.crew.length) {
        L.push('', 'Состав звена:');
        p.crew.forEach((id) => L.push('-' + empName(id)));
      }
    });
  });
  L.push(SEP, 'ИТОГО НА РАБОТЕ:', totalsLine(d));
  const shown = absentShown(d);
  ABSENT.forEach((st) => {
    const names = shown.filter((a) => S.emp.get(a.id).status === st).map((a) => empName(a.id));
    if (names.length) { L.push('', `${ABSENT_TITLE[st]}:`); names.forEach((n) => L.push('-' + n)); }
  });
  if (d.note.trim()) L.push('', 'ПРИМЕЧАНИЕ:', d.note.trim());
  return L.join('\n');
}

/* ───────────── автосохранение черновика ─────────────
   Каждое изменение сначала пишется на телефон, потом на сервер.
   Сервер не ответил — пробуем снова каждые 5 с; приложение свернули — отправляем сразу. */
let saveTimer = null; let retryTimer = null;
let saveInFlight = Promise.resolve();
let editSeq = 0; let syncedSeq = 0;
const SAVE_LABEL = { saving: 'сохраняю…', saved: '✓ сохранено', local: '⚠ нет связи — черновик на телефоне' };

function setSaveState(st) {
  S.saveState = st;
  const el = document.getElementById('save-state');
  if (el) { el.className = `save-state ${st}`; el.textContent = SAVE_LABEL[st] || ''; }
}
function snapshot() { const data = JSON.parse(JSON.stringify(S.draft)); data._ts = Date.now(); return data; }
function dirty() { return !!S.draft && editSeq !== syncedSeq; }
function scheduleSave() {
  editSeq++;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveDraftNow, 700);
}
function saveLocalNow() {
  if (S.draft) writeLocal({ data: snapshot(), ts: Date.now(), synced: !dirty() });
}
function stopSaving() {
  clearTimeout(saveTimer); clearTimeout(retryTimer); saveTimer = retryTimer = null;
}
function saveDraftNow() {
  stopSaving();
  if (!dirty() || S.fatal) return saveInFlight;
  const seq = editSeq; const data = snapshot();
  writeLocal({ data, ts: data._ts, synced: false });
  setSaveState('saving');
  saveInFlight = saveInFlight.catch(() => {}).then(() => api('PUT', '/api/draft', { data })).then(() => {
    syncedSeq = Math.max(syncedSeq, seq);
    if (S.boot && S.draft) S.boot.draft = { data, updated_at: new Date().toISOString() };
    if (seq === editSeq && S.draft) { writeLocal({ data, ts: data._ts, synced: true }); setSaveState('saved'); }
  }).catch(() => {
    if (S.fatal || !S.draft) return;
    setSaveState('local');
    if (!retryTimer) retryTimer = setTimeout(saveDraftNow, 5000);
  });
  return saveInFlight;
}
// Приложение сворачивают/закрывают — отправляем без ожидания (keepalive доживёт до сервера)
function flushDraft() {
  if (!dirty() || S.fatal) return;
  clearTimeout(saveTimer); saveTimer = null;
  const data = snapshot();
  writeLocal({ data, ts: data._ts, synced: false });
  apiOnce('PUT', '/api/draft', { data }, { keepalive: true, timeout: 8000 }).catch(() => {});
}
async function restoreLocalDraft() {
  const rec = await readLocal();
  if (!rec || !rec.data || !rec.data.objects) return;
  const server = S.boot.draft;
  const serverTs = (server && server.data && server.data._ts) || 0;
  if (rec.synced || (server && serverTs >= (rec.data._ts || 0))) {
    if (!server && rec.synced) writeLocal(null); // черновик отправлен или удалён — копия не нужна
    return;
  }
  // На телефоне есть изменения, которые не успели дойти до сервера
  S.boot.draft = { data: rec.data, updated_at: new Date(rec.ts || Date.now()).toISOString() };
  api('PUT', '/api/draft', { data: rec.data }).then(() => writeLocal({ ...rec, synced: true })).catch(() => {});
  toast('Восстановил несохранённый черновик', 3000);
}
function changed(rerender = false) {
  scheduleSave();
  if (rerender) render(); else updateLive();
}
function updateLive() {
  if (!S.draft) return;
  const t = document.getElementById('totals');
  if (t) t.innerHTML = totalsHtml(S.draft);
  const p = document.getElementById('preview');
  if (p) p.innerHTML = textHtml(buildText(S.draft));
}

/* ───────────── рендер ───────────── */
function render() {
  try { renderUnsafe(); } catch (e) {
    reportError(e);
    $app.innerHTML = `<div class="center-msg"><div class="big-emoji">😵</div><h2>Не получилось показать экран</h2>
      <p>${S.draft ? 'Черновик сохранён.' : ''}</p>
      <div class="row gap" style="justify-content:center;max-width:360px;margin:0 auto">
        <button class="btn btn-secondary" data-act="safe-home">На главную</button>
        <button class="btn btn-primary" onclick="location.reload()">Перезагрузить</button></div>
      <button class="link-btn" data-act="report">🐞 Сообщить о проблеме</button></div>`;
  }
}
let lastView = '';
function renderUnsafe() {
  const viewKey = `${S.tab}/${S.view}`;
  if (viewKey !== lastView) { trail('view', viewKey); lastView = viewKey; }
  const isEditorish = S.tab === 'disp' && S.view !== 'home';
  $tabs.classList.toggle('hidden', isEditorish);
  renderTabs();
  let html = '';
  if (S.tab === 'disp') html = S.view === 'editor' ? viewEditor() : S.view === 'dispatch' ? viewDispatch() : viewHome();
  else if (S.tab === 'staff') html = viewStaff();
  else if (S.tab === 'ciphers') html = viewCiphers();
  else if (S.tab === 'summary') html = viewSummary();
  else if (S.tab === 'access') html = viewAccess();
  $app.innerHTML = html;
  $app.querySelectorAll('textarea.auto').forEach(autoGrow);
  updateBackButton();
}
function autoGrow(el) { el.style.height = 'auto'; el.style.height = Math.max(44, el.scrollHeight + 3) + 'px'; }

function renderTabs() {
  const tabs = [['disp', '📝', 'Разнарядка'], ['staff', '👷', 'Люди']];
  if (isAdmin()) {
    const pend = (S.users || []).filter((u) => u.role === 'pending').length;
    tabs.push(['ciphers', '🏷️', 'Шифры'], ['summary', '📊', 'Сводка'], ['access', '🔑', 'Доступ' + (pend ? `<span class="dot">${pend}</span>` : '')]);
  }
  $tabs.innerHTML = tabs.map(([k, ic, t]) => `<button class="tab ${S.tab === k ? 'on' : ''}" data-act="tab" data-tab="${k}"><span class="ic">${ic}</span><span>${t}</span></button>`).join('');
}

function updateBackButton() {
  if (!tg || !tg.BackButton) return;
  const show = !!S.sheet || (S.tab === 'disp' && S.view !== 'home');
  if (show) tg.BackButton.show(); else tg.BackButton.hide();
}
function goBack() {
  if (S.sheet) { closeSheet(); return; }
  if (S.tab === 'disp' && S.view !== 'home') {
    if (S.view === 'editor' && S.draft) {
      S.boot.draft = { data: snapshot(), updated_at: new Date().toISOString() };
      saveDraftNow();
    }
    S.view = 'home'; S.dispatch = null; S.justSent = false; S.undo = null; S.offerClear = false;
    render(); window.scrollTo(0, 0);
  }
}

/* ── Главная ── */
function viewHome() {
  const b = S.boot;
  const dr = b.draft && b.draft.data;
  const setupHint = isAdmin() && (!b.ciphers.length || !b.employees.length)
    ? `<div class="card"><b>С чего начать</b><div class="hint" style="margin:4px 0 10px">Добавьте шифры объектов и штатку — после этого мастера смогут собирать разнарядки.</div>
       <div class="row gap">${!b.ciphers.length ? '<button class="btn btn-secondary sm" data-act="tab" data-tab="ciphers">🏷️ Шифры</button>' : ''}${!b.employees.length ? '<button class="btn btn-secondary sm" data-act="tab" data-tab="staff">👷 Штатка</button>' : ''}</div></div>` : '';
  const latest = b.latest;
  return `<div class="page">
    <div class="hello">Здравствуйте${b.me.master_name ? ', ' + esc(b.me.master_name) : (b.me.first_name ? ', ' + esc(b.me.first_name) : '')} 👋</div>
    ${setupHint}
    ${dr ? `<div class="card"><div><b>Черновик</b> на ${fmtDate(dr.date)} (${wd(dr.date)})</div>
      <div class="hint" style="margin:2px 0 10px">сохраняется автоматически</div>
      <div class="row gap"><button class="btn btn-primary" data-act="continue-draft">Продолжить</button><button class="btn btn-ghost danger" data-act="drop-draft">Удалить</button></div></div>` : ''}
    <button class="big-btn primary" data-act="new">＋ Новая разнарядка<span>на ${fmtDate(b.default_date)} (${wd(b.default_date)}) — дату можно поменять</span></button>
    ${latest ? `<button class="big-btn" data-act="copy-latest">⧉ Скопировать прошлую<span>от ${fmtDate(latest.date)} · ${esc(objList(latest.data))} — поправить и отправить</span></button>` : ''}
    <div class="section-title">Мои разнарядки</div>
    ${b.mine.length ? `<div class="list">${b.mine.map((m) => `
      <button class="list-item" data-act="open-dispatch" data-id="${m.id}">
        <div class="li-body"><div class="li-main"><b>${fmtDate(m.date)}</b> <span class="hint">${wd(m.date)}</span>
          ${m.date === today() ? ' <span class="badge">сегодня</span>' : ''}${m.date > today() ? ' <span class="badge">впереди</span>' : ''}</div>
          <div class="li-sub">${esc(m.master_name)} · ${esc(m.objects.join(' / ') || '—')} · ${m.workers} чел.</div></div>
        <span class="chev">›</span></button>`).join('')}</div>` : '<div class="empty">Пока нет ни одной разнарядки</div>'}
    <button class="link-btn" data-act="report">🐞 Сообщить о проблеме</button>
  </div>`;
}
function objList(data) { const s = []; (data.objects || []).forEach((o) => { if (o.short && !s.includes(o.short)) s.push(o.short); }); return s.join(' / ') || '—'; }

/* ── Редактор ── */
function viewEditor() {
  const d = S.draft;
  const dupe = S.boot.mine.find((m) => m.date === d.date && norm(m.master_name) === norm(d.master) && m.id !== d.id);
  const t = today();
  const chips = [[t, 'Сегодня'], [nextWorkday(t), null], [nextWorkday(nextWorkday(t)), null]];
  const chipHtml = chips.map(([iso, label]) => {
    const lbl = label || (iso === addDays(t, 1) ? `Завтра, ${wd(iso)}` : `${wd(iso)} ${fmtDate(iso).slice(0, 5)}`);
    return `<button class="chip ${d.date === iso ? 'on' : ''}" data-act="set-date" data-date="${iso}">${lbl}</button>`;
  }).join('');
  const filled = hasContent(d);
  const moved = d.id && d.orig_date && d.date !== d.orig_date;
  return `<div class="page with-bar">
    <div class="page-title">${d.id ? 'Изменение разнарядки' : 'Новая разнарядка'}
      <span id="save-state" class="save-state ${S.saveState}">${SAVE_LABEL[S.saveState] || ''}</span></div>
    <div class="card">
      <div class="lbl-row"><label class="lbl">Дата</label>
        ${filled ? '<button class="btn btn-ghost xs" data-act="clear-draft">🧹 Очистить</button>' : ''}</div>
      <div class="date-row"><input type="date" class="input" id="f-date" min="${t}" value="${d.date}"><span class="wd">${wd(d.date)}</span></div>
      <div class="chips">${chipHtml}</div>
      ${S.offerClear && filled ? `<div class="info-box"><div>Дата сменилась на <b>${fmtDate(d.date)} (${wd(d.date)})</b>. Заполнить разнарядку заново?</div>
        <div class="row gap"><button class="btn btn-primary sm" data-act="clear-now">🧹 Очистить и заполнить</button><button class="btn btn-secondary sm" data-act="keep-content">Оставить как есть</button></div></div>` : ''}
      ${S.undo ? `<div class="info-box ok"><div>Разнарядка очищена — дата, мастер и отметки БС/отпуск остались.</div>
        <div class="row gap"><button class="btn btn-secondary sm" data-act="undo-clear">↩ Вернуть как было</button><button class="btn btn-ghost sm" data-act="undo-hide">Скрыть</button></div></div>` : ''}
      ${moved ? `<div class="warn-box">Это уже отправленная разнарядка на <b>${fmtDate(d.orig_date)}</b>. После отправки она переедет на ${fmtDate(d.date)}, а на ${fmtDate(d.orig_date)} её не останется.
        <div class="row gap" style="margin-top:8px"><button class="btn btn-secondary sm" data-act="make-new">Сделать новой — ${fmtDate(d.orig_date).slice(0, 5)} останется</button></div></div>` : ''}
      ${dupe ? '<div class="warn-box">На эту дату у вас уже есть разнарядка этого мастера — при отправке она будет заменена.</div>' : ''}
      <label class="lbl">Мастер (кто составляет)</label>
      <input class="input" data-bind="master" placeholder="Например: Гедаев У." value="${esc(d.master)}" autocomplete="off">
    </div>
    ${d.objects.map(viewObject).join('')}
    <button class="btn btn-secondary wide" data-act="add-object" style="margin-bottom:12px">＋ Добавить объект</button>

    <div class="card">
      <label class="switch-row">
        <span class="txt"><b>ИТР выходит на работу</b><div class="hint">${esc(d.master.trim() || 'мастер')} — ${d.itr_on_work ? 'выходит' : 'не выходит'}</div></span>
        <span class="switch"><input type="checkbox" id="f-itr" ${d.itr_on_work ? 'checked' : ''}><span class="sl"></span></span>
      </label>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between"><b>БС / Отпуск / Межвахта</b><button class="btn btn-ghost sm" data-act="absent-add">＋ Отметить</button></div>
      <div class="hint">Эти люди не могут стоять в плане, пока не вернёте им статус «На работе».</div>
      ${viewAbsent(d)}
    </div>

    <div class="card">
      <label class="lbl">Примечание <span class="hint">(необязательно)</span></label>
      <textarea class="input auto" rows="2" data-bind="note" placeholder="Например: ${esc(NOTE_PRESET)}">${esc(d.note)}</textarea>
      ${d.note.trim() ? '' : `<div class="chips" style="margin-top:8px"><button class="chip" data-act="note-preset">＋ ${esc(NOTE_PRESET)}</button></div>`}
    </div>

    <div class="card totals" id="totals">${totalsHtml(d)}</div>
    <details class="card preview"><summary>👁 Предпросмотр текста</summary><pre id="preview">${textHtml(buildText(d))}</pre></details>
    <button class="link-btn" data-act="report">🐞 Сообщить о проблеме</button>
  </div>
  <div class="sticky-bar"><div class="inner"><button class="btn btn-primary" data-act="submit" id="btn-submit">Отправить разнарядку</button></div></div>`;
}

function totalsHtml(d) {
  const n = crewIds(d).size;
  return `<div class="num">${n}</div><div class="t-txt"><b>${plural(n, 'монтажник', 'монтажника', 'монтажников')} на работе</b><div class="hint">${d.itr_on_work ? '+ 1 ИТР (' + esc(d.master.trim() || 'мастер') + ')' : 'ИТР не выходит'}</div></div>`;
}

function viewObject(o, oi) {
  const d = S.draft;
  return `<div class="card obj">
    <div class="obj-head"><span class="obj-num">Объект ${oi + 1}</span>
      <button class="icon-btn danger" data-act="del-object" data-o="${oi}" aria-label="Удалить объект">🗑</button></div>
    <button class="obj-pick ${o.short ? '' : 'empty'}" data-act="pick-object" data-o="${oi}">
      ${o.short ? `<span class="obj-code">${esc(o.short)}</span><span class="obj-sub">${esc(o.building || (o.code !== o.short ? o.code : '') || 'сменить объект')}</span>` : '<span>Выбрать объект…</span>'}
      <span class="chev">›</span></button>
    ${o.plans.map((p, pi) => viewPlan(o, oi, p, pi)).join('')}
    <button class="btn btn-ghost wide sm" data-act="add-plan" data-o="${oi}" style="margin-top:8px">＋ Ещё план${o.short ? ' на ' + esc(o.short) : ''} (другое звено)</button>
  </div>`;
}

function viewPlan(o, oi, p, pi) {
  const multi = o.plans.length > 1;
  const crewOther = crewElsewhere(oi, pi);
  const crew = p.crew.map((id, ci) => {
    const e = S.emp.get(id);
    const busyBy = (S.busy[String(id)] || []);
    const bad = isAbsent(e);
    const warn = !bad && (crewOther.has(id) || busyBy.length);
    return `<span class="chip ${bad ? 'bad' : warn ? 'warn' : ''}" title="${esc(busyBy.join(', '))}">${esc(empName(id))}${bad ? ' · ' + esc(statusLabel(e.status)) : ''}${warn ? ' ⚠' : ''}<button class="x" data-act="crew-del" data-o="${oi}" data-p="${pi}" data-i="${ci}" aria-label="Убрать">×</button></span>`;
  }).join('');
  return `<div class="plan">
    <div class="plan-head"><span>План${multi ? ' ' + (pi + 1) : ''}</span>${multi ? `<button class="link danger" data-act="del-plan" data-o="${oi}" data-p="${pi}">Удалить план</button>` : ''}</div>
    ${p.works.map((w, wi) => viewWork(oi, pi, w, wi, p.works.length)).join('')}
    <button class="btn btn-ghost sm" data-act="add-work" data-o="${oi}" data-p="${pi}">＋ Работа</button>
    <div class="sub-lbl">Техника <span class="hint">(если нужна)</span></div>
    <div class="chips">${p.tech.map((t, ti) => `<span class="chip on">${esc(t)}<button class="x" data-act="tech-del" data-o="${oi}" data-p="${pi}" data-i="${ti}">×</button></span>`).join('')}
      <button class="chip add" data-act="pick-tech" data-o="${oi}" data-p="${pi}">＋ техника</button></div>
    <div class="sub-lbl">Состав звена · ${p.crew.length} чел.</div>
    <div class="crew">${crew}</div>
    <button class="btn btn-secondary sm" data-act="pick-crew" data-o="${oi}" data-p="${pi}">👷 ${p.crew.length ? 'Изменить состав' : 'Выбрать людей'}</button>
  </div>`;
}

function viewWork(oi, pi, w, wi, total) {
  const base = `objects.${oi}.plans.${pi}.works.${wi}`;
  const units = [...new Set([...UNITS, ...(S.boot.suggestions.units || [])])];
  if (w.unit && !units.includes(w.unit)) units.push(w.unit);
  const needUnit = String(w.volume).trim() && !w.unit;
  return `<div class="work">
    <span class="work-dot">•</span>
    <div class="work-body">
      <textarea class="input auto" rows="1" data-bind="${base}.name" data-sugg="1" placeholder="Наименование работы">${esc(w.name)}</textarea>
      <div class="sugg" id="sugg-${oi}-${pi}-${wi}"></div>
      <div class="work-vol">
        <input class="input vol" inputmode="decimal" data-bind="${base}.volume" placeholder="Объём (если есть)" value="${esc(w.volume)}" autocomplete="off">
        <select class="input unit ${needUnit ? 'need' : ''}" data-bind="${base}.unit">
          <option value="">ед.</option>${units.map((u) => `<option ${u === w.unit ? 'selected' : ''}>${esc(u)}</option>`).join('')}
        </select>
      </div>
    </div>
    ${total > 1 ? `<button class="icon-btn" data-act="del-work" data-o="${oi}" data-p="${pi}" data-w="${wi}" aria-label="Удалить работу">×</button>` : ''}
  </div>`;
}

function viewAbsent(d) {
  const shown = absentShown(d);
  if (!shown.length) return '<div class="empty" style="padding:10px 0 0">Никого — все на работе</div>';
  return ABSENT.map((st) => {
    const list = shown.filter((a) => S.emp.get(a.id).status === st);
    if (!list.length) return '';
    return `<div class="absent-group"><div class="g-lbl"><span class="status st-${st}">${statusLabel(st)}</span></div>
      <div class="chips">${list.map((a) => `<button class="chip" data-act="absent-edit" data-id="${a.id}">${esc(empName(a.id))} ›</button>`).join('')}</div></div>`;
  }).join('');
}

/** id сотрудника → где он уже стоит в этом черновике (кроме текущего плана). */
function crewElsewhere(oi, pi) {
  const m = new Map();
  S.draft.objects.forEach((o, i) => o.plans.forEach((p, j) => {
    if (i === oi && j === pi) return;
    p.crew.forEach((id) => m.set(id, `${o.short || 'объект ' + (i + 1)}${o.plans.length > 1 ? ', план ' + (j + 1) : ''}`));
  }));
  return m;
}

/* ── Просмотр разнарядки ── */
function viewDispatch() {
  const m = S.dispatch;
  const editable = m.date >= today() && m.author_id === S.boot.me.id;
  return `<div class="page">
    ${S.justSent ? '<div class="ok-box">✅ Разнарядка отправлена администратору</div>' : ''}
    <div class="page-title">Разнарядка на ${fmtDate(m.date)} <span class="hint">${wd(m.date)}</span></div>
    <button class="btn btn-primary wide big" data-act="copy-text">📋 Скопировать текст</button>
    <pre class="text-block">${textHtml(m.text)}</pre>
    <div class="grid2">
      <button class="btn btn-secondary" data-act="to-chat">💬 Прислать в чат</button>
      <button class="btn btn-secondary" data-act="copy-as-new">⧉ Копия на новую дату</button>
      ${editable ? '<button class="btn btn-secondary" data-act="edit-dispatch">✏️ Изменить</button>' : ''}
      <button class="btn btn-ghost danger" data-act="delete-dispatch">🗑 Удалить</button>
    </div>
    <button class="btn btn-ghost wide" data-act="home" style="margin-top:12px">На главную</button>
  </div>`;
}

/* ── Сотрудники ── */
function viewStaff() {
  const all = S.boot.employees;
  const cnt = (st) => all.filter((e) => e.status === st).length;
  const filters = [['all', 'Все', all.length], ['work', 'На работе', cnt('work')], ['bs', 'БС', cnt('bs')], ['otpusk', 'Отпуск', cnt('otpusk')], ['mezhvahta', 'Межвахта', cnt('mezhvahta')]];
  return `<div class="page">
    <div class="page-title">${isAdmin() ? 'Штатка' : 'Сотрудники'} <span class="hint">${all.length} чел.</span></div>
    ${isAdmin() ? `<div class="row gap" style="margin-bottom:10px"><button class="btn btn-primary sm" data-act="emp-add">＋ Добавить</button><button class="btn btn-secondary sm" data-act="emp-bulk">Вставить списком</button></div>` : '<div class="hint" style="margin:-6px 4px 10px">Нажмите на человека, чтобы сменить статус</div>'}
    <input class="input" placeholder="🔍 Поиск по ФИО или должности" data-filter="staffQuery" value="${esc(S.staffQuery)}" style="margin-bottom:8px">
    <div class="chips scroll" style="margin-bottom:10px">${filters.map(([k, t, n]) => `<button class="chip ${S.staffFilter === k ? 'on' : ''}" data-act="staff-filter" data-f="${k}">${t} · ${n}</button>`).join('')}</div>
    <div id="staff-list">${staffList()}</div>
  </div>`;
}
function staffList() {
  const q = norm(S.staffQuery);
  const list = S.boot.employees.filter((e) => (S.staffFilter === 'all' || e.status === S.staffFilter) && (!q || norm(e.full_name + ' ' + e.position).includes(q)));
  if (!S.boot.employees.length) return `<div class="empty">Штатка пуста.${isAdmin() ? ' Добавьте сотрудников кнопками выше — можно сразу списком из Excel.' : ' Её заполняет администратор.'}</div>`;
  if (!list.length) return '<div class="empty">Никого не найдено</div>';
  return `<div class="list">${list.map((e) => `<button class="list-item" data-act="emp-open" data-id="${e.id}">
    <div class="li-body"><div class="li-main">${esc(e.full_name)}</div><div class="li-sub">${esc(e.position || 'должность не указана')}</div></div>
    <span class="status st-${e.status}">${esc(statusLabel(e.status))}</span></button>`).join('')}</div>`;
}

/* ── Шифры (админ) ── */
function viewCiphers() {
  return `<div class="page">
    <div class="page-title">Шифры проектов <span class="hint">${S.boot.ciphers.length}</span></div>
    <button class="btn btn-primary wide" data-act="cipher-add" style="margin-bottom:10px">＋ Добавить шифры</button>
    <input class="input" placeholder="🔍 Поиск" data-filter="cipherQuery" value="${esc(S.cipherQuery)}" style="margin-bottom:10px">
    <div id="cipher-list">${cipherList()}</div>
  </div>`;
}
function cipherList() {
  const q = norm(S.cipherQuery);
  const list = S.boot.ciphers.filter((c) => !q || norm(c.code + ' ' + c.building).includes(q));
  if (!S.boot.ciphers.length) return '<div class="empty">Шифров пока нет. Объект в разнарядке берётся из шифра: AKU.0911.<b>91VTX</b>.CSB.AS.TB0004 → 91VTX</div>';
  if (!list.length) return '<div class="empty">Ничего не найдено</div>';
  return `<div class="list">${list.map((c) => `<button class="list-item" data-act="cipher-open" data-id="${c.id}">
    <div class="li-body"><div class="li-main"><b>${esc(c.short)}</b>${c.building ? ' <span class="hint">· ' + esc(c.building) + '</span>' : ''}</div><div class="li-sub code">${esc(c.code)}</div></div>
    <span class="chev">›</span></button>`).join('')}</div>`;
}

/* ── Сводка (админ) ── */
function viewSummary() {
  const t = today();
  const dates = [[t, 'Сегодня'], [nextWorkday(t), null]];
  const sm = S.summary;
  let body = '<div class="loading">Загрузка…</div>';
  if (sm && sm.date === S.summaryDate) {
    body = `<div class="stat-row">
        <div class="stat"><div class="v">${sm.dispatches.length}</div><div class="k">разнарядок</div></div>
        <div class="stat"><div class="v">${sm.workers}</div><div class="k">монтажников${sm.drivers ? `<br>вкл. ${sm.drivers} водит.` : ''}</div></div>
        <div class="stat"><div class="v">${sm.itr}</div><div class="k">ИТР</div></div></div>
      ${sm.missing.length ? `<div class="warn-box" style="margin:0 0 12px">Ещё не сдали: <b>${esc(sm.missing.join(', '))}</b></div>` : ''}
      ${sm.dispatches.length ? `<div class="list">${sm.dispatches.map((d) => `<button class="list-item" data-act="sum-open" data-id="${d.id}">
        <div class="li-body"><div class="li-main"><b>${esc(d.master_name)}</b>${d.itr_on_work ? '' : ' <span class="status st-bs">ИТР не выходит</span>'}</div>
        <div class="li-sub">${esc(d.objects.join(' / ') || '—')} · ${d.workers} чел. · ${esc(d.updated_at.slice(11, 16))}</div></div><span class="chev">›</span></button>`).join('')}</div>
        <button class="btn btn-primary wide big" data-act="sum-send" style="margin-top:12px">📊 Свести в Excel → в чат</button>
        <button class="btn btn-secondary wide big" data-act="sum-text" style="margin-top:8px">📝 Свести текстом → в чат</button>`
        : '<div class="empty">На эту дату разнарядок пока нет</div>'}`;
  }
  return `<div class="page">
    <div class="page-title">Сводка</div>
    <div class="card"><div class="date-row"><input type="date" class="input" id="f-sumdate" value="${S.summaryDate}"><span class="wd">${wd(S.summaryDate)}</span></div>
      <div class="chips" style="margin-top:8px">${dates.map(([iso, l]) => `<button class="chip ${S.summaryDate === iso ? 'on' : ''}" data-act="sum-date" data-date="${iso}">${l || (iso === addDays(t, 1) ? 'Завтра' : wd(iso) + ' ' + fmtDate(iso).slice(0, 5))}</button>`).join('')}
      <button class="chip" data-act="sum-date" data-date="${addDays(S.summaryDate, -1)}">← день</button><button class="chip" data-act="sum-date" data-date="${addDays(S.summaryDate, 1)}">день →</button></div></div>
    ${body}
  </div>`;
}
async function loadSummary() {
  const date = S.summaryDate;
  try {
    const r = await api('GET', `/api/admin/summary?date=${date}`);
    if (S.summaryDate === date) { S.summary = r; if (S.tab === 'summary') render(); }
  } catch (e) { toast(e.message); }
}

/* ── Доступ (админ) ── */
function viewAccess() {
  if (!S.users) return '<div class="page"><div class="loading">Загрузка…</div></div>';
  const who = (u) => `${esc([u.first_name, u.last_name].filter(Boolean).join(' ') || 'Без имени')}${u.username ? ' <span class="hint">@' + esc(u.username) + '</span>' : ''}`;
  const sub = (u) => `id ${u.tg_id}${u.master_name ? ' · подписывается: ' + esc(u.master_name) : ''}`;
  const group = (title, list, btns) => list.length ? `<div class="section-title">${title}</div><div class="list">${list.map((u) => `
    <div class="list-item" style="cursor:default"><div class="li-body"><div class="li-main">${who(u)}</div><div class="li-sub">${sub(u)}</div></div>${btns(u)}</div>`).join('')}</div>` : '';
  const pend = S.users.filter((u) => u.role === 'pending');
  const masters = S.users.filter((u) => u.role === 'master');
  const blocked = S.users.filter((u) => u.role === 'blocked');
  return `<div class="page">
    <div class="page-title">Доступ</div>
    <div class="hint" style="margin:-6px 4px 6px">Новый мастер нажимает /start в боте — заявка появится здесь и придёт вам в чат.</div>
    ${group('Заявки', pend, (u) => `<div class="row gap"><button class="btn btn-primary xs" data-act="role" data-id="${u.tg_id}" data-role="master">Дать</button><button class="btn btn-ghost xs danger" data-act="role" data-id="${u.tg_id}" data-role="blocked">Нет</button></div>`)}
    ${group('Мастера', masters, (u) => `<button class="btn btn-ghost xs danger" data-act="role" data-id="${u.tg_id}" data-role="blocked">Закрыть</button>`)}
    ${group('Доступ закрыт', blocked, (u) => `<button class="btn btn-secondary xs" data-act="role" data-id="${u.tg_id}" data-role="master">Вернуть</button>`)}
    ${!pend.length && !masters.length && !blocked.length ? '<div class="empty">Пока никто не подавал заявку</div>' : ''}
    <div class="section-title">Диагностика</div>
    <div class="card"><div class="hint" style="margin-bottom:10px">Что-то работает не так? Бот пришлёт архив с логами —
      перешлите его разработчику (в чат с Claude). Если бот не запускается — <b>collect_logs.bat</b> в папке бота.</div>
      <button class="btn btn-secondary wide" data-act="logs-send">📦 Прислать логи в чат</button></div>
  </div>`;
}
async function loadUsers() {
  try { S.users = await api('GET', '/api/admin/users'); render(); } catch (e) { toast(e.message); }
}

/* ───────────── шторка ───────────── */
function openSheet(opts) {
  S.sheet = opts;
  trail('sheet', String(opts.title).replace(/<[^>]+>/g, ''));
  $sheetRoot.innerHTML = `<div class="sheet-overlay" data-act="sheet-close"></div>
    <div class="sheet" role="dialog" aria-modal="true">
      <div class="sheet-head"><div class="sheet-title">${opts.title}</div><button class="icon-btn" data-act="sheet-close" aria-label="Закрыть">✕</button></div>
      ${opts.top ? `<div class="sheet-top">${opts.top}</div>` : ''}
      <div class="sheet-body" id="sheet-body"></div>
      <div class="sheet-foot" id="sheet-foot"></div>
    </div>`;
  document.body.classList.add('noscroll');
  refreshSheet();
  updateBackButton();
}
function refreshSheet() {
  if (!S.sheet) return;
  const b = document.getElementById('sheet-body');
  const f = document.getElementById('sheet-foot');
  if (b) b.innerHTML = S.sheet.body();
  if (f) f.innerHTML = S.sheet.foot ? S.sheet.foot() : '';
  $sheetRoot.querySelectorAll('textarea.auto').forEach(autoGrow);
}
function closeSheetQuiet() {
  S.sheet = null; $sheetRoot.innerHTML = ''; document.body.classList.remove('noscroll');
}
function closeSheet() {
  const s = S.sheet; S.sheet = null;
  $sheetRoot.innerHTML = '';
  document.body.classList.remove('noscroll');
  if (s && s.onClose) s.onClose();
  updateBackButton();
}

/* Выбор объекта */
function openObjectPicker(oi) {
  let q = '';
  const recent = new Set(S.boot.latest ? (S.boot.latest.data.objects || []).map((o) => o.short) : []);
  openSheet({
    title: 'Объект',
    top: '<input class="input" data-sheet-q placeholder="🔍 91VTX, 05UZU, СМБ…" autocomplete="off">',
    body: () => {
      const nq = norm(q);
      let list = S.boot.ciphers.filter((c) => !nq || norm(c.code + ' ' + c.building).includes(nq));
      list = [...list.filter((c) => recent.has(c.short)), ...list.filter((c) => !recent.has(c.short))];
      let h = list.map((c) => `<button class="pick-row" data-act="pick" data-id="${c.id}">
        <div class="pr-main"><div class="pr-name"><b>${esc(c.short)}</b>${c.building ? ' · ' + esc(c.building) : ''}${recent.has(c.short) ? ' <span class="badge">недавно</span>' : ''}</div><div class="pr-sub code">${esc(c.code)}</div></div></button>`).join('');
      if (q.trim() && !list.some((c) => norm(c.short) === nq)) {
        h += `<button class="pick-row" data-act="custom"><div class="pr-main"><div class="pr-name">Использовать «<b>${esc(shortCode(q.trim()))}</b>»</div><div class="pr-sub">объект не из списка шифров</div></div></button>`;
      }
      if (!h) h = '<div class="empty">Шифров пока нет. Введите объект вручную в поиске (например, СМБ).</div>';
      return h;
    },
    onInput: (el) => { q = el.value; refreshSheet(); },
    actions: {
      pick: (el) => {
        const c = S.boot.ciphers.find((x) => x.id === Number(el.dataset.id));
        Object.assign(S.draft.objects[oi], { cipher_id: c.id, code: c.code, short: c.short, building: c.building });
        haptic('select'); closeSheet(); changed(true);
      },
      custom: () => {
        const v = q.trim();
        Object.assign(S.draft.objects[oi], { cipher_id: null, code: v, short: shortCode(v), building: '' });
        closeSheet(); changed(true);
      },
    },
  });
}

/* Выбор людей в звено */
function openCrewPicker(oi, pi) {
  let q = '';
  const o = S.draft.objects[oi];
  openSheet({
    title: `Состав звена · ${esc(o.short || 'объект ' + (oi + 1))}${o.plans.length > 1 ? ', план ' + (pi + 1) : ''}`,
    top: '<input class="input" data-sheet-q placeholder="🔍 Поиск по фамилии" autocomplete="off">',
    body: () => {
      const plan = S.draft.objects[oi].plans[pi];
      const sel = new Set(plan.crew);
      const other = crewElsewhere(oi, pi);
      const nq = norm(q);
      const list = S.boot.employees.filter((e) => !nq || norm(e.full_name + ' ' + e.position).includes(nq))
        .sort((a, b) => (isAbsent(a) - isAbsent(b)) || byName(a, b));
      if (!S.boot.employees.length) return '<div class="empty">Штатка пуста — её заполняет администратор.</div>';
      if (!list.length) return '<div class="empty">Никого не найдено</div>';
      return list.map((e) => {
        if (isAbsent(e)) {
          return `<div class="pick-row off"><span class="check"></span><div class="pr-main"><div class="pr-name">${esc(e.full_name)}</div><div class="pr-sub">${esc(e.position)}</div></div>
            <span class="status st-${e.status}">${esc(statusLabel(e.status))}</span><button class="btn btn-secondary xs" data-act="to-work" data-id="${e.id}">На работу</button></div>`;
        }
        const notes = [];
        if (other.has(e.id)) notes.push(`<span class="w">уже: ${esc(other.get(e.id))}</span>`);
        const b = S.busy[String(e.id)];
        if (b && b.length) notes.push(`<span class="w">у ${esc(b.join(', '))}</span>`);
        return `<button class="pick-row ${sel.has(e.id) ? 'on' : ''}" data-act="toggle" data-id="${e.id}"><span class="check">${sel.has(e.id) ? '✓' : ''}</span>
          <div class="pr-main"><div class="pr-name">${esc(e.full_name)}</div><div class="pr-sub">${[esc(e.position), ...notes].filter(Boolean).join(' · ')}</div></div></button>`;
      }).join('');
    },
    foot: () => `<button class="btn btn-primary wide" data-act="sheet-close">Готово · ${S.draft.objects[oi].plans[pi].crew.length} чел.</button>`,
    onInput: (el) => { q = el.value; refreshSheet(); },
    actions: {
      toggle: (el) => {
        const id = Number(el.dataset.id);
        const crew = S.draft.objects[oi].plans[pi].crew;
        const i = crew.indexOf(id);
        if (i >= 0) crew.splice(i, 1); else crew.push(id);
        haptic('select'); scheduleSave(); refreshSheet();
      },
      'to-work': async (el) => {
        const e = S.emp.get(Number(el.dataset.id));
        if (!(await confirmBox(`Сменить статус «${e.full_name}» с «${statusLabel(e.status)}» на «На работе»?`))) return;
        el.disabled = true; el.textContent = '…';
        try { await setStatus(e.id, 'work'); } finally { refreshSheet(); }
        S.draft.absent = S.draft.absent.filter((a) => a.id !== e.id);
        scheduleSave();
      },
    },
    onClose: () => changed(true),
  });
}

/* Техника */
function openTechPicker(oi, pi) {
  const plan = S.draft.objects[oi].plans[pi];
  openSheet({
    title: 'Техника',
    top: '<div class="row gap"><input class="input" id="tech-custom" placeholder="Своя техника…" autocomplete="off"><button class="btn btn-secondary" data-act="add-custom" style="flex:none">Добавить</button></div>',
    body: () => {
      const all = [...new Set([...TECH, ...(S.boot.suggestions.tech || []), ...plan.tech])];
      return `<div class="chips" style="padding-top:4px">${all.map((t) => `<button class="chip ${plan.tech.includes(t) ? 'on' : ''}" data-act="toggle" data-t="${esc(t)}">${plan.tech.includes(t) ? '✓ ' : ''}${esc(t)}</button>`).join('')}</div>`;
    },
    foot: () => '<button class="btn btn-primary wide" data-act="sheet-close">Готово</button>',
    actions: {
      toggle: (el) => { const t = el.dataset.t; const i = plan.tech.indexOf(t); if (i >= 0) plan.tech.splice(i, 1); else plan.tech.push(t); haptic('select'); refreshSheet(); },
      'add-custom': () => {
        const inp = document.getElementById('tech-custom'); const v = inp.value.trim();
        if (v && !plan.tech.includes(v)) plan.tech.push(v);
        inp.value = ''; refreshSheet();
      },
    },
    onClose: () => changed(true),
  });
}

/* Отметить отсутствующего */
function openAbsentPicker() {
  let q = ''; let chosen = null;
  const d = S.draft;
  openSheet({
    title: 'Кто не выходит',
    top: '<input class="input" data-sheet-q placeholder="🔍 Поиск по фамилии" autocomplete="off">',
    body: () => {
      if (chosen) {
        const e = S.emp.get(chosen);
        return `<div style="padding:6px 2px"><b>${esc(e.full_name)}</b><div class="hint">${esc(e.position)}</div></div>
          <div class="status-btns">${ABSENT.map((st) => `<button class="btn" data-act="set" data-st="${st}"><span class="status st-${st}">${statusLabel(st)}</span></button>`).join('')}
          <button class="btn btn-ghost" data-act="back">← Назад</button></div>`;
      }
      const listed = new Set(absentShown(d).map((a) => a.id));
      const nq = norm(q);
      const list = S.boot.employees.filter((e) => !listed.has(e.id) && (!nq || norm(e.full_name).includes(nq)));
      if (!list.length) return '<div class="empty">Никого не найдено</div>';
      return list.map((e) => `<button class="pick-row" data-act="choose" data-id="${e.id}"><div class="pr-main"><div class="pr-name">${esc(e.full_name)}</div><div class="pr-sub">${esc(e.position)}</div></div><span class="status st-${e.status}">${esc(statusLabel(e.status))}</span></button>`).join('');
    },
    onInput: (el) => { q = el.value; refreshSheet(); },
    actions: {
      choose: (el) => { chosen = Number(el.dataset.id); refreshSheet(); },
      back: () => { chosen = null; refreshSheet(); },
      set: async (el) => {
        await setStatus(chosen, el.dataset.st);
        if (!d.absent.some((a) => a.id === chosen)) d.absent.push({ id: chosen });
        const removedFrom = removeFromCrews(chosen);
        closeSheet(); changed(true);
        if (removedFrom) toast(`${empName(chosen)} убран(а) из плана`);
      },
    },
  });
}
function openAbsentEdit(id) {
  const e = S.emp.get(id);
  openSheet({
    title: esc(e.full_name),
    body: () => `<div class="hint" style="margin-bottom:6px">Сейчас: ${esc(statusLabel(e.status))}</div>
      <div class="status-btns">${['work', ...ABSENT].map((st) => `<button class="btn ${e.status === st ? 'cur' : ''}" data-act="set" data-st="${st}"><span class="status st-${st}">${statusLabel(st)}</span></button>`).join('')}</div>`,
    actions: {
      set: async (el) => {
        const st = el.dataset.st;
        await setStatus(id, st);
        if (st === 'work') S.draft.absent = S.draft.absent.filter((a) => a.id !== id);
        closeSheet(); changed(true);
      },
    },
  });
}
function removeFromCrews(id) {
  let n = 0;
  S.draft.objects.forEach((o) => o.plans.forEach((p) => { const i = p.crew.indexOf(id); if (i >= 0) { p.crew.splice(i, 1); n++; } }));
  return n;
}
async function setStatus(id, status) {
  try {
    // повтор безопасен: тот же статус второй раз ничего не меняет
    const e = await api('POST', `/api/employees/${id}/status`, { status }, { retry: true });
    updateEmployee(e);
    if (ABSENT.includes(status)) { if (!S.boot.absent_by_me.includes(id)) S.boot.absent_by_me.push(id); }
    else S.boot.absent_by_me = S.boot.absent_by_me.filter((x) => x !== id);
    haptic('success');
    return e;
  } catch (err) {
    haptic('error');
    if (!err.shown) await alertBox(`Статус не изменён: ${err.message}`);
    err.shown = true;
    throw err;
  }
}

/* Сотрудник (статус / редактирование) */
function openEmployee(id) {
  const e = S.emp.get(id);
  const admin = isAdmin();
  openSheet({
    title: admin ? 'Сотрудник' : esc(e.full_name),
    body: () => `${admin ? `<label class="lbl">ФИО</label><input class="input" id="e-name" value="${esc(e.full_name)}">
        <label class="lbl">Должность</label><input class="input" id="e-pos" value="${esc(e.position)}" list="dl-pos" placeholder="Монтажник">${positionsDatalist()}` : `<div class="hint">${esc(e.position || '')}</div>`}
      <label class="lbl">Статус</label>
      <div class="status-btns">${['work', ...ABSENT].map((st) => `<button class="btn ${S.emp.get(id).status === st ? 'cur' : ''}" data-act="set" data-st="${st}"><span class="status st-${st}">${statusLabel(st)}</span></button>`).join('')}</div>`,
    foot: admin ? () => '<div class="row gap"><button class="btn btn-ghost danger" data-act="del">Удалить</button><button class="btn btn-primary" data-act="save">Сохранить</button></div>' : null,
    actions: {
      set: async (el) => { await setStatus(id, el.dataset.st); if (admin) refreshSheet(); else { closeSheet(); render(); } },
      save: async () => {
        const full_name = document.getElementById('e-name').value.trim();
        const position = document.getElementById('e-pos').value.trim();
        if (!full_name) { toast('Введите ФИО'); return; }
        try { S.boot.employees = await api('PUT', `/api/admin/employees/${id}`, { full_name, position }); indexEmployees(); closeSheet(); render(); toast('Сохранено'); } catch (err) { alertBox(err.message); }
      },
      del: async () => {
        if (!(await confirmBox(`Удалить «${e.full_name}» из штатки? В старых разнарядках он останется.`))) return;
        try { S.boot.employees = await api('DELETE', `/api/admin/employees/${id}`); indexEmployees(); closeSheet(); render(); toast('Удалено'); } catch (err) { alertBox(err.message); }
      },
    },
    onClose: () => { if (S.tab === 'staff') render(); },
  });
}
function positionsDatalist() {
  const pos = [...new Set(['Монтажник', 'Сварщик', 'Электрогазосварщик', 'Разнорабочий', 'Бетонщик', 'Арматурщик', 'Водитель', ...S.boot.employees.map((e) => e.position).filter(Boolean)])];
  return `<datalist id="dl-pos">${pos.map((p) => `<option value="${esc(p)}">`).join('')}</datalist>`;
}
function openEmployeeAdd() {
  openSheet({
    title: 'Новый сотрудник',
    body: () => `<label class="lbl">ФИО</label><input class="input" id="e-name" placeholder="Иванов Алексей" autocomplete="off">
      <label class="lbl">Должность</label><input class="input" id="e-pos" list="dl-pos" placeholder="Монтажник" autocomplete="off">${positionsDatalist()}`,
    foot: () => '<div class="row gap"><button class="btn btn-secondary" data-act="add-more">Добавить ещё</button><button class="btn btn-primary" data-act="add">Добавить</button></div>',
    actions: {
      add: async () => { if (await addOne()) closeSheet(); },
      'add-more': async () => { if (await addOne()) { document.getElementById('e-name').value = ''; document.getElementById('e-name').focus(); } },
    },
    onClose: () => render(),
  });
  async function addOne() {
    const full_name = document.getElementById('e-name').value.trim();
    const position = document.getElementById('e-pos').value.trim() || 'Монтажник';
    if (!full_name) { toast('Введите ФИО'); return false; }
    try { const r = await api('POST', '/api/admin/employees', { items: [{ full_name, position }] }); S.boot.employees = r.employees; indexEmployees(); toast(`Добавлен: ${full_name}`); haptic('success'); return true; } catch (err) { alertBox(err.message); return false; }
  }
}
function parseStaffLines(text, defPos) {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split(/\t|;|\s[—–-]\s/).map((s) => s.trim()).filter(Boolean);
    let name = parts[0] || ''; let pos = parts.slice(1).join(' ') || defPos;
    name = name.replace(/^\d+[.)]?\s+/, '');
    return { full_name: name, position: pos };
  }).filter((x) => x.full_name && !/^ф\.?и\.?о/i.test(x.full_name));
}
function openEmployeeBulk() {
  let text = ''; let defPos = 'Монтажник';
  openSheet({
    title: 'Вставить списком',
    top: '<div class="hint">По одному человеку на строку. Можно скопировать столбец из Excel. Должность — через «;» или Tab: <i>Иванов Алексей; Сварщик</i></div>',
    body: () => `<textarea class="input" id="bulk" rows="8" placeholder="Сыздиков Шавкатжан&#10;Абдуллаев Шохрух&#10;Иванов Алексей; Сварщик" style="overflow:auto;min-height:180px">${esc(text)}</textarea>
      <label class="lbl">Должность по умолчанию</label><input class="input" id="bulk-pos" list="dl-pos" value="${esc(defPos)}">${positionsDatalist()}
      <div class="hint" id="bulk-count" style="margin-top:8px">${parseStaffLines(text, defPos).length} чел.</div>`,
    foot: () => '<button class="btn btn-primary wide" data-act="save">Добавить всех</button>',
    onInput: (el) => {
      if (el.id === 'bulk') text = el.value; if (el.id === 'bulk-pos') defPos = el.value;
      document.getElementById('bulk-count').textContent = `${parseStaffLines(text, defPos).length} чел.`;
    },
    actions: {
      save: async () => {
        const items = parseStaffLines(text, defPos.trim() || 'Монтажник');
        if (!items.length) { toast('Список пуст'); return; }
        try { const r = await api('POST', '/api/admin/employees', { items }); S.boot.employees = r.employees; indexEmployees(); closeSheet(); render(); toast(`Добавлено: ${r.added}`); haptic('success'); } catch (err) { alertBox(err.message); }
      },
    },
  });
}

/* Шифры */
function parseCipherLines(text, building) {
  return text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean).map((line) => {
    const parts = line.split(/\t|;/).map((s) => s.trim()).filter(Boolean);
    return { code: parts[0] || '', building: parts[1] || building || '' };
  }).filter((x) => x.code);
}
function openCipherAdd() {
  let text = ''; let building = '';
  openSheet({
    title: 'Добавить шифры',
    top: '<div class="hint">По одному шифру на строку. Сооружение — через «;» или Tab, либо общее поле ниже.</div>',
    body: () => {
      const items = parseCipherLines(text, building);
      return `<textarea class="input" id="c-bulk" rows="6" placeholder="AKU.0911.91VTX.CSB.AS.TB0004&#10;AKU.0911.03UZU.CSB.KZ.TB0001; Площадка выдачи электромощности" style="overflow:auto;min-height:140px">${esc(text)}</textarea>
        <label class="lbl">Сооружение / здание <span class="hint">(для Excel, необязательно)</span></label>
        <input class="input" id="c-building" value="${esc(building)}" placeholder="Временное ограждение">
        <div id="c-preview" style="margin-top:10px">${cipherPreview(items)}</div>`;
    },
    foot: () => '<button class="btn btn-primary wide" data-act="save">Добавить</button>',
    onInput: (el) => {
      if (el.id === 'c-bulk') text = el.value; if (el.id === 'c-building') building = el.value;
      document.getElementById('c-preview').innerHTML = cipherPreview(parseCipherLines(text, building));
    },
    actions: {
      save: async () => {
        const items = parseCipherLines(text, building);
        if (!items.length) { toast('Введите хотя бы один шифр'); return; }
        try {
          const r = await api('POST', '/api/admin/ciphers', { items });
          S.boot.ciphers = r.ciphers; closeSheet(); render();
          toast(`Добавлено: ${r.added}${r.skipped.length ? `, уже были: ${r.skipped.length}` : ''}`); haptic('success');
        } catch (err) { alertBox(err.message); }
      },
    },
  });
}
function cipherPreview(items) {
  if (!items.length) return '';
  return `<div class="hint" style="margin-bottom:6px">В разнарядке будет так:</div><div class="chips">${items.slice(0, 40).map((i) => `<span class="chip on">${esc(shortCode(i.code))}</span>`).join('')}</div>`;
}
function openCipher(id) {
  const c = S.boot.ciphers.find((x) => x.id === id);
  openSheet({
    title: 'Шифр',
    body: () => `<label class="lbl">Шифр</label><input class="input" id="c-code" value="${esc(c.code)}">
      <div class="hint" style="margin:6px 2px">В разнарядке: <b id="c-short">${esc(shortCode(c.code))}</b></div>
      <label class="lbl">Сооружение / здание</label><input class="input" id="c-building" value="${esc(c.building)}" placeholder="необязательно">`,
    foot: () => '<div class="row gap"><button class="btn btn-ghost danger" data-act="del">Удалить</button><button class="btn btn-primary" data-act="save">Сохранить</button></div>',
    onInput: (el) => { if (el.id === 'c-code') document.getElementById('c-short').textContent = shortCode(el.value); },
    actions: {
      save: async () => {
        try { S.boot.ciphers = await api('PUT', `/api/admin/ciphers/${id}`, { code: document.getElementById('c-code').value, building: document.getElementById('c-building').value }); closeSheet(); render(); toast('Сохранено'); } catch (err) { alertBox(err.message); }
      },
      del: async () => {
        if (!(await confirmBox(`Удалить шифр ${c.code}?`))) return;
        try { S.boot.ciphers = await api('DELETE', `/api/admin/ciphers/${id}`); closeSheet(); render(); } catch (err) { alertBox(err.message); }
      },
    },
  });
}

/* Сообщить о проблеме: описание + журнал действий + текущий черновик */
function openReport() {
  let text = '';
  const reportId = uid() + Date.now().toString(36);
  openSheet({
    title: '🐞 Сообщить о проблеме',
    top: '<div class="hint">Опишите, что делали и что пошло не так. Вместе с описанием администратору уйдёт технический журнал приложения — по нему быстрее найти причину.</div>',
    body: () => `<textarea class="input" id="rep-text" rows="5" placeholder="Например: нажал «На работу» у Иванова — выскочила ошибка 404" style="min-height:130px;overflow:auto">${esc(text)}</textarea>`,
    foot: () => '<button class="btn btn-primary wide" data-act="send">Отправить администратору</button>',
    onInput: (el) => { if (el.id === 'rep-text') text = el.value; },
    actions: {
      send: async (el) => {
        if (!text.trim()) { toast('Опишите проблему хотя бы парой слов'); return; }
        el.disabled = true; el.textContent = 'Отправляю…';
        try {
          await api('POST', '/api/report', {
            id: reportId, text: text.trim(), view: lastView, env: clientEnv(), trail: TRAIL.slice(), draft: S.draft ? snapshot() : null,
          }, { retry: true, timeout: 30000 });
          closeSheet(); haptic('success');
          await alertBox('Спасибо! Сообщение ушло администратору вместе с журналом приложения.');
        } catch (err) {
          el.disabled = false; el.textContent = 'Отправить администратору';
          if (!err.shown) await alertBox(`Не отправилось: ${err.message}`);
        }
      },
    },
  });
}

/* ───────────── действия ───────────── */
function idx(el, k) { return Number(el.dataset[k]); }

async function openEditor(draft, removed = []) {
  S.draft = draft; S.tab = 'disp'; S.view = 'editor'; S.busy = {};
  S.undo = null; S.offerClear = false; S.submitReq = null; S.saveState = '';
  render(); window.scrollTo(0, 0);
  scheduleSave();
  loadBusy();
  if (removed.length) alertBox('Убраны из плана, т.к. сейчас не на работе:\n' + removed.join('\n'));
}
async function loadBusy() {
  if (!S.draft) return;
  const date = S.draft.date;
  try {
    const b = await api('GET', `/api/busy?date=${date}`);
    if (!S.draft || S.draft.date !== date) return;
    S.busy = b || {};
    const typing = document.activeElement && $app.contains(document.activeElement) && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && document.activeElement.id !== 'f-date';
    if (S.view === 'editor' && !S.sheet && !typing && Object.keys(S.busy).length) render();
  } catch (_) { /* не критично */ }
}
function setDraftDate(iso) {
  if (!iso || !S.draft) return;
  if (iso < today()) { toast('Нельзя прошедшей датой'); iso = today(); }
  if (iso === S.draft.date) { render(); return; }
  S.draft.date = iso;
  S.offerClear = hasContent(S.draft); // новая дата — предложить начать с чистого листа
  S.undo = null;
  changed(true); loadBusy();
}
function clearDraftContent() {
  S.undo = JSON.parse(JSON.stringify(S.draft));
  S.draft.objects = [emptyObject()]; S.draft.note = ''; S.draft.itr_on_work = true;
  S.offerClear = false;
  haptic('success'); changed(true); window.scrollTo(0, 0);
}

function validate(d) {
  const errs = [];
  if (!d.master.trim()) errs.push('Укажите мастера (кто составляет разнарядку).');
  if (d.date < today()) errs.push('Нельзя сделать разнарядку прошедшей датой.');
  if (!d.objects.length) errs.push('Добавьте хотя бы один объект.');
  d.objects.forEach((o, oi) => {
    const name = o.short || `Объект ${oi + 1}`;
    if (!o.short) errs.push(`Объект ${oi + 1}: выберите объект.`);
    o.plans.forEach((p, pi) => {
      if (!p.works.some((w) => w.name.trim())) errs.push(`${name}${o.plans.length > 1 ? ', план ' + (pi + 1) : ''}: впишите хотя бы одну работу.`);
      p.crew.forEach((id) => { const e = S.emp.get(id); if (isAbsent(e)) errs.push(`${e.full_name} — ${statusLabel(e.status)}, не может стоять в плане.`); });
    });
  });
  return errs;
}

async function submit() {
  const d = S.draft;
  const errs = validate(d);
  if (errs.length) { haptic('error'); await alertBox(errs.slice(0, 6).join('\n')); return; }
  const warns = [];
  d.objects.forEach((o, oi) => o.plans.forEach((p, pi) => { if (!p.crew.length) warns.push(`${o.short || 'Объект ' + (oi + 1)}${o.plans.length > 1 ? ', план ' + (pi + 1) : ''}: никого нет в звене`); }));
  const dupe = S.boot.mine.find((m) => m.date === d.date && norm(m.master_name) === norm(d.master) && m.id !== d.id);
  if (dupe) warns.push(`Разнарядка на ${fmtDate(d.date)} уже есть — она будет заменена`);
  if (warns.length && !(await confirmBox(warns.join('\n') + '\n\nОтправить?'))) return;

  const text = buildText(d);
  const data = {
    date: d.date, master: d.master.trim(), itr_on_work: d.itr_on_work, note: d.note,
    absent: absentShown(d).map((a) => ({ id: a.id })),
    objects: d.objects.map((o) => ({
      cipher_id: o.cipher_id, code: o.code, short: o.short, building: o.building,
      plans: o.plans.map((p) => ({
        works: p.works.filter((w) => w.name.trim()).map((w) => ({ name: w.name.trim(), volume: String(w.volume).trim(), unit: w.unit })),
        crew: p.crew.map((id) => ({ id, name: empName(id) })),
        tech: p.tech,
      })),
    })),
  };
  const btn = document.getElementById('btn-submit');
  if (btn) { btn.disabled = true; btn.textContent = 'Отправляю…'; }
  stopSaving();
  saveLocalNow();
  await Promise.race([saveInFlight.catch(() => {}), sleep(3000)]);
  // Один и тот же req_id для повторов той же самой разнарядки: если ответ потерялся,
  // сервер не создаст вторую и не пришлёт админу второй пуш
  const sig = JSON.stringify([d.id, data, text]);
  if (!S.submitReq || S.submitReq.sig !== sig) S.submitReq = { sig, id: uid() + uid() + Date.now().toString(36) };
  try {
    const r = await api('POST', '/api/dispatches', { id: d.id, data, text, req_id: S.submitReq.id, draft_ts: Date.now() },
      { retry: true, timeout: 30000 });
    haptic('success');
    S.submitReq = null;
    S.boot.me.master_name = data.master;
    S.boot.draft = null; S.draft = null; S.undo = null; S.offerClear = false;
    writeLocal(null); syncedSeq = editSeq; S.saveState = '';
    await refreshMine();
    S.dispatch = r.dispatch; S.justSent = true; S.view = 'dispatch';
    render(); window.scrollTo(0, 0);
  } catch (err) {
    haptic('error');
    if (btn) { btn.disabled = false; btn.textContent = 'Отправить разнарядку'; }
    if (S.draft) scheduleSave();
    if (!err.shown) await alertBox(`Разнарядка не отправлена: ${err.message}\n\nЧерновик сохранён — нажмите «Отправить» ещё раз.`);
  }
}
async function refreshMine() {
  try {
    S.boot.mine = await api('GET', '/api/dispatches');
    if (S.boot.mine.length) S.boot.latest = await api('GET', `/api/dispatches/${S.boot.mine[0].id}`);
    else S.boot.latest = null;
  } catch (_) { /* оставим как есть */ }
}

const ACTIONS = {
  tab: (el) => {
    const t = el.dataset.tab;
    if (S.tab === 'disp' && S.view === 'editor') goBack();
    S.tab = t; if (t === 'disp') S.view = 'home';
    if (t === 'summary') { S.summary = null; loadSummary(); }
    if (t === 'access') loadUsers();
    render(); window.scrollTo(0, 0); haptic('select');
  },
  home: () => { S.view = 'home'; S.dispatch = null; S.justSent = false; render(); window.scrollTo(0, 0); },

  new: async () => {
    if (S.boot.draft && !(await confirmBox('Есть несохранённый черновик. Удалить его и начать новую разнарядку?'))) return;
    openEditor(newDraft());
  },
  'copy-latest': () => {
    if (!S.boot.latest) return;
    const { draft, removed } = draftFrom(S.boot.latest.data, { date: S.boot.default_date });
    openEditor(draft, removed);
  },
  'continue-draft': () => {
    const data = S.boot.draft.data;
    const { draft, removed } = draftFrom(data, { id: data.id || null, date: data.date });
    openEditor(draft, removed);
  },
  'drop-draft': async () => {
    if (!(await confirmBox('Удалить черновик?'))) return;
    S.boot.draft = null; S.draft = null; stopSaving(); writeLocal(null); render();
    await api('DELETE', `/api/draft?ts=${Date.now()}`, undefined, { retry: true }).catch(() => {});
  },
  'clear-draft': async () => {
    if (!(await confirmBox('Очистить разнарядку и заполнить заново?\n\nУдалятся объекты, работы, люди и примечание. Дата, мастер и отметки БС/отпуск/межвахта останутся.'))) return;
    clearDraftContent();
  },
  'clear-now': () => clearDraftContent(),
  'keep-content': () => { S.offerClear = false; render(); },
  'undo-clear': () => {
    const u = S.undo; if (!u || !S.draft) return;
    S.undo = null;
    S.draft.objects = u.objects; S.draft.note = u.note; S.draft.itr_on_work = u.itr_on_work;
    changed(true); toast('Вернул как было');
  },
  'undo-hide': () => { S.undo = null; render(); },
  'make-new': () => { S.draft.id = null; S.draft.orig_date = null; changed(true); toast('Будет отдельной разнарядкой — прежняя останется'); },
  report: () => openReport(),
  'logs-send': async (el) => {
    el.disabled = true; el.textContent = 'Собираю…';
    try { await api('POST', '/api/admin/logs/send', {}, { timeout: 90000 }); haptic('success'); toast('Архив логов отправлен в чат с ботом'); } catch (err) { if (!err.shown) alertBox(err.message); }
    el.disabled = false; el.textContent = '📦 Прислать логи в чат';
  },
  'close-app': () => { try { tg.close(); } catch (_) { location.reload(); } },
  'safe-home': () => { closeSheetQuiet(); S.tab = 'disp'; S.view = 'home'; S.dispatch = null; render(); window.scrollTo(0, 0); },
  'open-dispatch': async (el) => {
    try { S.dispatch = await api('GET', `/api/dispatches/${el.dataset.id}`); S.justSent = false; S.view = 'dispatch'; render(); window.scrollTo(0, 0); } catch (err) { toast(err.message); }
  },
  'copy-text': async () => {
    const ok = await copyText(S.dispatch.text);
    if (ok) { haptic('success'); toast('Текст скопирован ✅'); } else { await alertBox('Не удалось скопировать автоматически. Нажмите «Прислать в чат» — бот пришлёт текст сообщением, его можно переслать.'); }
  },
  'to-chat': async () => {
    try { await api('POST', `/api/dispatches/${S.dispatch.id}/to_chat`); haptic('success'); toast('Отправил текст в чат с ботом'); } catch (err) { toast(err.message); }
  },
  'copy-as-new': async () => {
    if (S.boot.draft && !(await confirmBox('Есть черновик. Заменить его копией этой разнарядки?'))) return;
    const { draft, removed } = draftFrom(S.dispatch.data, { date: S.boot.default_date });
    openEditor(draft, removed);
  },
  'edit-dispatch': async () => {
    if (S.boot.draft && !(await confirmBox('Есть черновик. Заменить его этой разнарядкой для правки?'))) return;
    const { draft, removed } = draftFrom(S.dispatch.data, { id: S.dispatch.id, date: S.dispatch.date });
    openEditor(draft, removed);
  },
  'delete-dispatch': async () => {
    if (!(await confirmBox(`Удалить разнарядку на ${fmtDate(S.dispatch.date)}?`))) return;
    try { await api('DELETE', `/api/dispatches/${S.dispatch.id}`); await refreshMine(); S.view = 'home'; S.dispatch = null; render(); toast('Удалено'); } catch (err) { toast(err.message); }
  },

  'set-date': (el) => { setDraftDate(el.dataset.date); haptic('select'); },
  'add-object': () => { S.draft.objects.push(emptyObject()); changed(true); openObjectPicker(S.draft.objects.length - 1); },
  'del-object': async (el) => {
    const oi = idx(el, 'o'); const o = S.draft.objects[oi];
    const hasContent = o.short || o.plans.some((p) => p.crew.length || p.works.some((w) => w.name.trim()));
    if (hasContent && !(await confirmBox(`Удалить объект ${o.short || oi + 1} со всеми планами?`))) return;
    S.draft.objects.splice(oi, 1);
    if (!S.draft.objects.length) S.draft.objects.push(emptyObject());
    changed(true);
  },
  'pick-object': (el) => openObjectPicker(idx(el, 'o')),
  'add-plan': (el) => { S.draft.objects[idx(el, 'o')].plans.push(emptyPlan()); changed(true); },
  'del-plan': async (el) => {
    const o = S.draft.objects[idx(el, 'o')];
    if (!(await confirmBox('Удалить этот план?'))) return;
    o.plans.splice(idx(el, 'p'), 1); changed(true);
  },
  'add-work': (el) => {
    const oi = idx(el, 'o'), pi = idx(el, 'p');
    const works = S.draft.objects[oi].plans[pi].works; works.push(emptyWork()); changed(true);
    const ta = document.querySelector(`[data-bind="objects.${oi}.plans.${pi}.works.${works.length - 1}.name"]`);
    if (ta) ta.focus();
  },
  'del-work': (el) => { S.draft.objects[idx(el, 'o')].plans[idx(el, 'p')].works.splice(idx(el, 'w'), 1); changed(true); },
  'pick-crew': (el) => openCrewPicker(idx(el, 'o'), idx(el, 'p')),
  'crew-del': (el) => { S.draft.objects[idx(el, 'o')].plans[idx(el, 'p')].crew.splice(idx(el, 'i'), 1); haptic('select'); changed(true); },
  'pick-tech': (el) => openTechPicker(idx(el, 'o'), idx(el, 'p')),
  'tech-del': (el) => { S.draft.objects[idx(el, 'o')].plans[idx(el, 'p')].tech.splice(idx(el, 'i'), 1); changed(true); },
  'absent-add': () => openAbsentPicker(),
  'absent-edit': (el) => openAbsentEdit(Number(el.dataset.id)),
  'note-preset': () => { S.draft.note = NOTE_PRESET; changed(true); },
  'sugg-pick': (el) => {
    const path = el.dataset.path; setPath(S.draft, path, el.dataset.v);
    const ta = document.querySelector(`[data-bind="${path}"]`); if (ta) { ta.value = el.dataset.v; autoGrow(ta); }
    el.closest('.sugg').innerHTML = ''; changed(false);
  },
  submit: () => submit(),

  'staff-filter': (el) => { S.staffFilter = el.dataset.f; render(); },
  'emp-open': (el) => openEmployee(Number(el.dataset.id)),
  'emp-add': () => openEmployeeAdd(),
  'emp-bulk': () => openEmployeeBulk(),
  'cipher-add': () => openCipherAdd(),
  'cipher-open': (el) => openCipher(Number(el.dataset.id)),

  'sum-date': (el) => { S.summaryDate = el.dataset.date; S.summary = null; render(); loadSummary(); },
  'sum-open': (el) => {
    const d = S.summary.dispatches.find((x) => x.id === Number(el.dataset.id));
    openSheet({
      title: esc(d.master_name),
      body: () => `<pre class="text-block" style="margin:0;background:var(--bg)">${textHtml(d.text)}</pre>`,
      foot: () => '<button class="btn btn-primary wide" data-act="copy">📋 Скопировать текст</button>',
      actions: { copy: async () => { if (await copyText(d.text)) { haptic('success'); toast('Скопировано ✅'); } else toast('Не удалось скопировать'); } },
    });
  },
  'sum-send': async (el) => {
    el.disabled = true; el.textContent = 'Формирую…';
    try { await api('POST', '/api/admin/summary/send', { date: S.summaryDate }, { timeout: 60000 }); haptic('success'); toast('Excel отправлен в чат с ботом'); } catch (err) { alertBox(err.message); }
    el.disabled = false; el.textContent = '📊 Свести в Excel → в чат';
  },
  'sum-text': async (el) => {
    el.disabled = true; el.textContent = 'Формирую…';
    try { await api('POST', '/api/admin/summary/text', { date: S.summaryDate }, { timeout: 30000 }); haptic('success'); toast('Текстовая сводка отправлена в чат с ботом'); } catch (err) { alertBox(err.message); }
    el.disabled = false; el.textContent = '📝 Свести текстом → в чат';
  },
  role: async (el) => {
    try { S.users = await api('POST', `/api/admin/users/${el.dataset.id}/role`, { role: el.dataset.role }, { retry: true }); haptic('success'); render(); } catch (err) { toast(err.message); }
  },
};

/* ───────────── события ───────────── */
document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  trail('tap', act + (el.dataset.id ? ` #${el.dataset.id}` : '') + (el.dataset.date ? ` ${el.dataset.date}` : '') + (el.dataset.tab ? ` ${el.dataset.tab}` : ''));
  if (el.closest('#sheet-root')) {
    if (act === 'sheet-close') { closeSheet(); return; }
    const fn = S.sheet && S.sheet.actions && S.sheet.actions[act];
    if (fn) { ev.preventDefault(); Promise.resolve().then(() => fn(el, ev)).catch(reportError); }
    return;
  }
  const fn = ACTIONS[act];
  if (fn) { ev.preventDefault(); Promise.resolve().then(() => fn(el, ev)).catch(reportError); }
});

/* Свернули/закрыли приложение — дописываем черновик на сервер; вернулась связь — досылаем */
document.addEventListener('visibilitychange', () => {
  trail('app', document.visibilityState === 'hidden' ? 'свернули' : 'вернулись');
  if (document.visibilityState === 'hidden') flushDraft(); else if (dirty()) saveDraftNow();
});
window.addEventListener('pagehide', flushDraft);
function updateNet() {
  trail('net', navigator.onLine === false ? 'нет интернета' : 'онлайн');
  const el = document.getElementById('net');
  if (el) el.hidden = navigator.onLine !== false;
}
window.addEventListener('offline', updateNet);
window.addEventListener('online', () => { updateNet(); if (dirty()) saveDraftNow(); });

document.addEventListener('input', (ev) => {
  const el = ev.target;
  if (el.closest('#sheet-root')) {
    if (el.matches('textarea.auto')) autoGrow(el);
    if (S.sheet && S.sheet.onInput) S.sheet.onInput(el, ev);
    return;
  }
  if (el.dataset.filter) {
    S[el.dataset.filter] = el.value;
    const box = document.getElementById(el.dataset.filter === 'staffQuery' ? 'staff-list' : 'cipher-list');
    if (box) box.innerHTML = el.dataset.filter === 'staffQuery' ? staffList() : cipherList();
    return;
  }
  if (el.dataset.bind && S.draft) {
    setPath(S.draft, el.dataset.bind, el.value);
    if (el.matches('textarea.auto')) autoGrow(el);
    if (el.dataset.sugg) showSuggestions(el);
    if (el.classList.contains('vol')) {
      const sel = el.parentElement.querySelector('select.unit');
      if (sel) sel.classList.toggle('need', !!el.value.trim() && !sel.value);
    }
    changed(false);
  }
});

document.addEventListener('change', (ev) => {
  const el = ev.target;
  if (el.id === 'f-date') { setDraftDate(el.value); return; }
  if (el.id === 'f-itr') { S.draft.itr_on_work = el.checked; haptic('select'); changed(true); return; }
  if (el.id === 'f-sumdate') { if (el.value) { S.summaryDate = el.value; S.summary = null; render(); loadSummary(); } return; }
  if (el.tagName === 'SELECT' && el.dataset.bind && S.draft) {
    setPath(S.draft, el.dataset.bind, el.value); el.classList.remove('need'); changed(false);
  }
});

document.addEventListener('focusout', (ev) => {
  const el = ev.target;
  if (el.dataset && el.dataset.sugg) {
    const box = el.parentElement.querySelector('.sugg');
    setTimeout(() => { if (box) box.innerHTML = ''; }, 250);
  }
});

function showSuggestions(el) {
  const box = el.parentElement.querySelector('.sugg');
  if (!box) return;
  const q = norm(el.value);
  if (q.length < 2) { box.innerHTML = ''; return; }
  const words = q.split(' ');
  const items = (S.boot.suggestions.works || []).filter((w) => { const n = norm(w); return n !== q && words.every((x) => n.includes(x)); }).slice(0, 5);
  box.innerHTML = items.length ? `<div class="sugg-list">${items.map((w) => `<button class="sugg-item" data-act="sugg-pick" data-path="${el.dataset.bind}" data-v="${esc(w)}">${esc(w)}</button>`).join('')}</div>` : '';
}
document.addEventListener('pointerdown', (ev) => { if (ev.target.closest('.sugg-item')) ev.preventDefault(); });

/* ───────────── запуск ───────────── */
function viewPending() {
  return `<div class="center-msg"><div class="big-emoji">⏳</div><h2>Нужен доступ</h2>
    <p>Заявка отправлена администратору. Как только он подтвердит — бот пришлёт сообщение.</p>
    <p class="hint">Ваш Telegram ID: ${S.boot.me.id}</p>
    <button class="btn btn-secondary" onclick="location.reload()">Проверить снова</button></div>`;
}

async function boot() {
  if (tg) {
    tg.ready(); tg.expand();
    try { if (tgv('7.7')) tg.disableVerticalSwipes(); } catch (_) { /* старый клиент */ }
    try { if (tgv('6.1')) { tg.setHeaderColor('secondary_bg_color'); tg.setBackgroundColor('secondary_bg_color'); } } catch (_) { /* нет */ }
    if (tg.BackButton) tg.BackButton.onClick(goBack);
    try { if (tgv('8.0')) tg.onEvent('deactivated', flushDraft); } catch (_) { /* старый клиент */ }
  }
  trail('boot', `app ${APP_V}, Telegram ${tg ? tg.version : '-'} ${tg ? tg.platform : ''}`);
  updateNet();
  if (!tg || !tg.initData) {
    $app.innerHTML = '<div class="center-msg"><div class="big-emoji">📱</div><h2>Откройте в Telegram</h2><p>Это мини-приложение бота — запустите его кнопкой «Разнарядка» в чате с ботом.</p></div>';
    return;
  }
  try { S.boot = await api('GET', '/api/bootstrap', undefined, { timeout: 25000 }); } catch (e) {
    if (e.shown) return;
    $app.innerHTML = `<div class="center-msg"><div class="big-emoji">⚠️</div><h2>Не удалось загрузить</h2><p>${esc(e.message)}</p><button class="btn btn-secondary" onclick="location.reload()">Повторить</button></div>`;
    return;
  }
  if (!['admin', 'master'].includes(S.boot.me.role)) { $app.innerHTML = viewPending(); return; }
  indexEmployees();
  try { await restoreLocalDraft(); } catch (e) { reportError(e); }
  S.summaryDate = S.boot.default_date;
  if (isAdmin()) api('GET', '/api/admin/users').then((u) => { S.users = u; renderTabs(); }).catch(() => {});
  render();
}
boot().catch(reportError);
