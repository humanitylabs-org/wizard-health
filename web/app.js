'use strict';
// Health: one metric at a time (Recovery, Sleep or Activity) for the selected day in one plain sentence, over a
// GitHub-style contribution graph of every day, read from the box's health-data connectors.
// Reads only this app's documented relative API; never talks to a provider and never takes keys.
const el = id => document.getElementById(id);
const SVG = 'http://www.w3.org/2000/svg';
const DAY = 86400000;
let ST = null, SUM = null, DAYS = [], BY = {}, pollTimer = null;
let sel = null;            // the selected date (ISO); null = the newest day ("today")
let METRIC = 'recovery';   // what colours the squares
let hoverIso = null;

// ---------- small helpers ----------
function say(text, kind = '') {
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
}
async function api(path, data) {
  const init = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const r = await fetch(path, init);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(body.error || 'Health could not do that. Try again.');
  return body;
}
function h(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
function s(tag, attrs) { const n = document.createElementNS(SVG, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; }
const num = v => typeof v === 'number' && isFinite(v);
const DASH = '\u2013';
const hm = hours => { if (!num(hours)) return DASH; const m = Math.round(hours * 60); return Math.floor(m / 60) + 'h ' + (m % 60) + 'm'; };
const addDays = (iso, n) => new Date(Date.parse(iso + 'T12:00:00Z') + n * DAY).toISOString().slice(0, 10);
const dow = iso => new Date(iso + 'T12:00:00Z').getUTCDay();
const localIso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const noon = iso => new Date(iso + 'T12:00:00');
const short = iso => noon(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
function dayName(iso) {
  // The box clock is UTC; name the day by the owner's own calendar.
  const now = new Date();
  if (iso === localIso(now)) return 'Today';
  if (iso === localIso(new Date(now.getTime() - DAY))) return 'Yesterday';
  return noon(iso).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
}
function ago(iso) {
  if (!iso) return 'never';
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return 'just now'; if (m < 60) return m + ' min ago';
  const hrs = Math.round(m / 60); return hrs < 36 ? hrs + ' h ago' : Math.round(hrs / 24) + ' days ago';
}
const cap = t => t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
const mean = xs => { const v = xs.filter(num); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };

// ---------- opening a connector app ----------
// Inside Wizard both shells are same-origin and answer the documented wizard:open event (native bridge v0);
// anywhere else the plain link opens the app in this window.
function openApp(e, id, path) {
  try {
    const top = window.top;
    if (top !== window && top.wizardUX && top.location.origin === location.origin) {
      top.dispatchEvent(new CustomEvent('wizard:open', { detail: { app: id, path } }));
      e.preventDefault();
    }
  } catch (_) { /* not same-origin: follow the link */ }
}
function appLink(text, id, path, cls = 'wz-button') {
  const a = h('a', cls, text); a.href = path; a.addEventListener('click', e => openApp(e, id, path));
  return a;
}

// ---------- sources ----------
const live = src => src && ['connected', 'syncing', 'rate_limited'].includes(src.status);
const primary = () => ST && ST.sources.find(x => x.primary);
const hasData = () => !!(ST && ST.sources.some(x => x.days > 0));
const STATUS = { connected: ['Connected', 'ok'], syncing: ['Syncing', 'ok'], rate_limited: ['Paused', 'warn'], needs_login: ['Sign in again', 'warn'],
  not_configured: ['Not connected', 'off'], not_installed: ['Not installed', 'off'], needs_update: ['Needs an update', 'warn'] };
// Fresh = the source is signed in and synced within the last 3 hours.
const fresh = src => live(src) && src.last_sync && Date.now() - new Date(src.last_sync) < 3 * 3600000;

// ---------- the numbers, in plain words (one record shape: the summary or a stored day) ----------
const labels = () => (SUM && SUM.labels) || {};
const newest = () => (SUM && SUM.available && SUM.day) || (primary() || {}).latest_date || null;
const shown = () => sel || newest();
// The record for a date: the summary for its own day (it fills an unscored newest day from the last scored one).
const rec4 = iso => (SUM && SUM.available && SUM.day === iso) ? SUM : BY[iso] || null;
const recov = d => ((d || {}).readiness || {}).score;
const sleepH = d => ((d || {}).sleep || {}).asleep_h;
const needH = d => ((d || {}).sleep || {}).need_h;
const act = d => ((d || {}).load || {}).value;
const actMax = d => ((d || {}).load || {}).max || labels().load_max || 21;
const hrv = d => (d || {}).hrv_ms;
// WHOOP's recovery bands: 0-33 red, 34-66 yellow, 67-100 green.
const recBand = v => !num(v) ? null : v >= 67 ? 'hi' : v >= 34 ? 'md' : 'lo';
const REC_WORD = { hi: 'High', md: 'Medium', lo: 'Low' };
// WHOOP's strain bands: under 10, 10-14, 14-18, 18 and over.
const actWord = v => !num(v) ? null : v >= 18 ? 'All out' : v >= 14 ? 'High' : v >= 10 ? 'Moderate' : 'Light';
const actLevel = v => !num(v) ? 0 : v >= 18 ? 5 : v >= 14 ? 4 : v >= 10 ? 3 : v >= 5 ? 2 : 1;
const sleepLevel = v => !num(v) ? 0 : v >= 8 ? 5 : v >= 7 ? 4 : v >= 6 ? 3 : v >= 5 ? 2 : 1;

// What each square means for each metric: its class, its tooltip words and the legend.
const SCALE = {
  recovery: {
    name: 'Recovery',
    cls: d => { const b = recBand(recov(d)); return b ? 'c-' + b : ''; },
    tip: d => num(recov(d)) ? 'Recovery ' + Math.round(recov(d)) + '%' : null,
    legend: [['Low', ['c-lo', 'c-md', 'c-hi'], 'High']],
    keys: [['c-lo', 'Low, 0\u201333%'], ['c-md', 'Medium, 34\u201366%'], ['c-hi', 'High, 67\u2013100%']],
  },
  sleep: {
    name: 'Sleep',
    cls: d => { const l = sleepLevel(sleepH(d)); return l ? 'c-g c-' + l : ''; },
    tip: d => num(sleepH(d)) ? 'Sleep ' + hm(sleepH(d)) : null,
    keys: [['c-g c-1', 'Under 5 hours'], ['c-g c-2', '5 to 6 hours'], ['c-g c-3', '6 to 7 hours'], ['c-g c-4', '7 to 8 hours'], ['c-g c-5', '8 hours or more']],
  },
  activity: {
    name: 'Activity',
    cls: d => { const l = actLevel(act(d)); return l ? 'c-a c-' + l : ''; },
    tip: d => num(act(d)) ? 'Activity ' + act(d).toFixed(1) : null,
    keys: [['c-a c-1', 'Light, under 5'], ['c-a c-2', 'Light, 5 to 10'], ['c-a c-3', 'Moderate, 10 to 14'], ['c-a c-4', 'High, 14 to 18'], ['c-a c-5', 'All out, 18 or more']],
  },
};

// ---------- the focus card: one metric, one day, one plain sentence ----------
// A duration in words-light form: 4h 23m, or 40m under an hour.
const dur = hours => { const m = Math.round(hours * 60); return m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm'; };
// Sleep against need: Enough at 85% or more, A bit short at 70-85%, Short below 70%.
const sleepBand = (h, need) => !num(h) || !num(need) || need <= 0 ? null : h / need >= 0.85 ? 'hi' : h / need >= 0.70 ? 'md' : 'lo';
const SLEEP_WORD = { hi: 'Enough', md: 'A bit short', lo: 'Short' };
const REC_SAY = {
  now: { hi: 'Your body is well recovered. A good day to push.',
    md: 'Your body is partly recovered. A normal day is fine; skip anything very hard.',
    lo: "Your body hasn't recovered. Take it easy today." },
  past: { hi: 'Your body was well recovered. A good day to push.',
    md: 'Your body was partly recovered. A normal day was fine; nothing very hard.',
    lo: "Your body hadn't recovered. A day to take it easy." },
};
const ACT_SAY = { 'Light': "A light day. You moved, but didn't push hard.", 'Moderate': 'A moderate day. You put in some real effort.',
  'High': 'A hard day. Your heart worked a lot.', 'All out': 'An all-out day. Your body will need time to recover.' };
// Everything the focus card shows for one metric on one day, generated from the values.
function focusOf(metric, d, past) {
  if (metric === 'recovery') {
    const r = recov(d), b = recBand(r);
    if (!b) return { value: DASH, say: 'Not scored yet. Your wearable scores recovery after you wake up.', more: 'Why?' };
    return { value: Math.round(r) + '%', chip: REC_WORD[b], chipCls: 'c-' + b, say: REC_SAY[past ? 'past' : 'now'][b], more: 'Why?' };
  }
  if (metric === 'sleep') {
    const sh = sleepH(d), nd = needH(d), b = sleepBand(sh, nd);
    if (!num(sh)) return { value: DASH, say: 'No sleep recorded for this night.', more: 'More' };
    if (!b) return { value: hm(sh), say: 'You slept ' + dur(sh) + '.', more: 'More' };
    const missing = Math.round(nd * 60) - Math.round(sh * 60);
    return { value: hm(sh), of: 'of ' + hm(nd) + ' needed', chip: SLEEP_WORD[b], chipCls: 'c-' + b, bar: Math.min(1, sh / nd),
      say: missing > 0 ? 'You slept ' + dur(missing / 60) + ' less than your body needed.' : 'You got all the sleep your body needed.', more: 'More' };
  }
  const a = act(d), w = actWord(a);
  if (!w) return { value: DASH, say: 'No activity recorded for this day.', more: 'More' };
  return { value: a.toFixed(1), of: 'of ' + actMax(d), chip: w, chipCls: 'c-a c-' + Math.max(2, actLevel(a)), say: ACT_SAY[w], more: 'More' };
}

// ---------- the screen ----------
function renderAll() {
  const data = hasData();
  el('empty').hidden = data;
  el('screen').hidden = !data;
  if (!data) { renderEmpty(); return; }
  const p = primary(), ok = p && fresh(p);
  const dot = el('srcdot');
  dot.className = 'hl-srcdot ' + (ok ? 'ok' : 'stale');
  dot.setAttribute('aria-label', p ? `Sources: ${p.name} ${ok ? 'up to date' : live(p) ? 'synced ' + ago(p.last_sync) : (STATUS[p.status] || ['not connected'])[0].toLowerCase()}` : 'Sources');
  if (sel && (!newest() || sel > newest())) sel = null;
  renderFocus();
  renderGraph();
}
function renderEmpty() {
  const c = ST.connectors.find(x => x.installed) || null;
  const a = el('connect');
  a.href = c ? c.app_path : '#';
  a.onclick = e => { if (c) openApp(e, c.id, c.app_path); else { e.preventDefault(); openSources(); } };
  el('wait').hidden = !ST.sources.some(live);
}
function setChip(c, F) {
  c.textContent = F.chip || ''; c.className = 'hl-chip' + (F.chipCls ? ' ' + F.chipCls : ''); c.hidden = !F.chip;
}
function renderFocus() {
  const iso = shown(); if (!iso) return;
  const F = focusOf(METRIC, rec4(iso) || {}, !!sel);
  el('date').textContent = dayName(iso);
  el('today').hidden = !sel;
  el('fnum').textContent = F.value;
  el('fof').textContent = F.of || '';
  setChip(el('fchip'), F);
  const bar = el('fbar');
  bar.hidden = F.bar == null;
  if (F.bar != null) { bar.className = 'hl-bar ' + (F.chipCls || ''); bar.firstChild.style.width = (F.bar * 100).toFixed(1) + '%'; }
  el('fsay').textContent = F.say;
  const more = el('fmore');
  more.textContent = F.more;
  more.setAttribute('aria-label', (F.more === 'Why?' ? 'Why? ' : 'More about ') + SCALE[METRIC].name.toLowerCase());
  el('focus').dataset.m = METRIC;
  el('focus').setAttribute('aria-label', `${SCALE[METRIC].name}, ${dayName(iso)}: ${F.value}${F.of ? ' ' + F.of : ''}${F.chip ? ', ' + F.chip : ''}. ${F.say}`);
}

// ---------- the graph ----------
let G = { start: null, weeks: 0, end: null };
function renderGraph() {
  const last = newest(); if (!last) return;
  const today = localIso(new Date());
  const endDay = last > today ? last : today;
  const end = addDays(endDay, 6 - dow(endDay));                  // the Saturday closing the newest week
  const dates = DAYS.map(d => d.date).sort();
  const first = dates[0] && dates[0] < last ? dates[0] : last;
  // Only the weeks that hold data plus the current week (no grey lead-in); a history wider than the window scrolls
  // sideways (a phone), opening on today.
  const weeks = Math.max(1, Math.round((Date.parse(end) - Date.parse(addDays(first, -dow(first)))) / DAY + 1) / 7);
  const start = addDays(end, -(weeks * 7 - 1));                  // the Sunday opening the oldest week
  const rebuilt = G.start !== start || G.weeks !== weeks;
  G = { start, weeks, end, endDay };
  const cells = el('cells'), months = el('months');
  if (rebuilt) {
    cells.replaceChildren(); months.replaceChildren();
    let lastMonthCol = -9;
    for (let i = 0; i < weeks * 7; i++) {
      const iso = addDays(start, i);
      if (iso > endDay) { cells.append(h('i', 'hl-cell hl-future')); continue; }
      const b = h('button', 'hl-cell'); b.type = 'button'; b.dataset.d = iso;
      cells.append(b);
      // Month names sit over the first week that starts in that month, as on GitHub.
      if (i % 7 === 0) {
        const col = i / 7, wkMonth = iso.slice(0, 7), prev = col ? addDays(iso, -7).slice(0, 7) : null;
        if ((col === 0 && iso.slice(8) <= '24') || (col > 0 && wkMonth !== prev)) {
          if (col - lastMonthCol >= 2) { const m = h('span', '', noon(iso).toLocaleDateString('en-US', { month: 'short' })); m.style.setProperty('--col', col); months.append(m); lastMonthCol = col; }
        }
      }
    }
  }
  const S = SCALE[METRIC], on = shown();
  let tracked = 0;
  for (const b of cells.children) {
    const iso = b.dataset.d; if (!iso) continue;
    const d = rec4(iso), tip = d && S.tip(d);
    if (d) tracked++;
    b.className = 'hl-cell ' + (d ? S.cls(d) : '') + (iso === on ? ' on' : '');
    b.setAttribute('aria-label', short(iso) + ': ' + (tip || 'no data'));
    b.setAttribute('aria-pressed', String(iso === on));
  }
  renderLegend();
  for (const b of el('metric').children) b.setAttribute('aria-checked', String(b.dataset.m === METRIC));
  const scrolls = sizeGraph();
  el('cap').textContent = tracked + (tracked === 1 ? ' day' : ' days') + ' tracked' + (scrolls ? ' \u00b7 older weeks to the left' : '');
  if (rebuilt) scrollToDay(on, true);
  fitMonths();
  showTip();
}
function renderLegend() {
  const S = SCALE[METRIC], box = el('legend');
  box.replaceChildren(h('span', '', 'Less'));
  for (const [cls, title] of S.keys) { const i = h('i', 'hl-cell ' + cls); i.title = title; box.append(i); }
  box.append(h('span', '', 'More'));
  if (METRIC === 'recovery') { box.firstChild.textContent = 'Low'; box.lastChild.textContent = 'High'; }
  box.setAttribute('aria-label', S.name + ': ' + S.keys.map(k => k[1]).join(', '));
}
// Square size from the window's width (the scroller is as wide as its card, never as wide as its content):
// every week fits when it can, in squares as big as fit; otherwise whole weeks of thumb-sized squares (22 px or more
// on a narrow window) fill the view and older weeks scroll sideways.
const GAP = 3;
const narrowWin = () => el('main').clientWidth < 600;
function sizeGraph() {
  const sc = el('gscroll'), avail = sc.clientWidth - 4;           // .hl-gin's side padding
  if (avail <= 0 || !G.weeks) return false;
  const narrow = narrowWin(), min = narrow ? 22 : 15, max = narrow ? 36 : 40;
  let cell = Math.floor((avail + GAP) / G.weeks) - GAP, vis = G.weeks;
  if (cell < min) { vis = Math.max(1, Math.floor((avail + GAP) / (min + GAP))); cell = Math.floor((avail + GAP) / vis) - GAP; }
  cell = Math.max(10, Math.min(cell, max));
  const scrolls = G.weeks > vis;
  G.vis = vis; G.step = cell + GAP;
  const g = el('graph');
  g.style.setProperty('--cell', cell + 'px');
  g.style.setProperty('--gap', GAP + 'px');
  // When scrolling, the rounding remainder pads the right end so whole weeks line up with both edges (no sliver).
  el('gin').style.paddingRight = scrolls ? Math.max(2, avail - (vis * G.step - GAP) + 2) + 'px' : '';
  g.classList.toggle('hl-scrolls', scrolls);
  return scrolls;
}
// A month name cut by either edge of the visible squares, or crowding the next one, is hidden (no stray "un").
function fitMonths() {
  const sc = el('gscroll'), L = sc.scrollLeft, R = L + sc.clientWidth, W = el('cells').offsetWidth + 2;
  let next = Infinity;
  for (const m of [...el('months').children].reverse()) {
    const x = m.offsetLeft, w = m.offsetWidth;
    const hide = x < L - 1 || x + w > Math.min(R, W + 2) + 1 || x + w + 6 > next;
    m.style.visibility = hide ? 'hidden' : '';
    if (!hide) next = x;
  }
}
// Bring a day's week into view, keeping whole weeks at both edges.
function scrollToDay(iso, instant) {
  const sc = el('gscroll'), b = iso && el('cells').querySelector(`[data-d="${iso}"]`);
  if (!b || !G.step) return;
  const col = Math.round((b.offsetLeft - 2) / G.step), first = Math.round(sc.scrollLeft / G.step), vis = G.vis || G.weeks;
  let left = null;
  if (col < first) left = col;
  else if (col > first + vis - 1) left = col - vis + 1;
  if (instant && col >= G.weeks - 1) left = Math.max(0, G.weeks - vis);   // opening on today: the newest weeks, flush
  if (left != null) sc.scrollTo({ left: Math.max(0, left * G.step), behavior: instant ? 'instant' : 'smooth' });
}
// GitHub-style tooltip over the square under the mouse (taps show the day in the focus card instead).
function showTip() {
  const tip = el('tip');
  const iso = hoverIso;
  const b = iso && el('cells').querySelector(`[data-d="${iso}"]`);
  if (!b) { tip.hidden = true; return; }
  const d = rec4(iso), t = d && SCALE[METRIC].tip(d);
  tip.textContent = short(iso) + ' \u00b7 ' + (t || 'No data');
  tip.hidden = false;
  const g = el('graph').getBoundingClientRect(), r = b.getBoundingClientRect(), sc = el('gscroll').getBoundingClientRect();
  if (r.right < sc.left || r.left > sc.right) { tip.hidden = true; return; }
  const tw = tip.offsetWidth;
  let x = r.left + r.width / 2 - g.left - tw / 2;
  x = Math.max(4, Math.min(g.width - tw - 4, x));
  tip.style.left = x + 'px';
  tip.style.top = (r.top - g.top - tip.offsetHeight - 6) + 'px';
  tip.style.setProperty('--ax', (r.left + r.width / 2 - g.left - x) + 'px');
}
function pick(iso) {
  const n = newest();
  if (!iso || !n || iso > n) return;
  sel = iso === n ? null : iso;
  renderFocus(); renderGraph();
}
// Switching metric keeps the selected day.
function setMetric(m) {
  if (!SCALE[m] || m === METRIC) return;
  METRIC = m;
  try { localStorage.setItem(PREF, m); } catch (e) {}
  renderFocus(); renderGraph();
}

// ---------- the detail sheet (one dialog, WzSheet closes it five ways) ----------
function openSheet(title, nodes, kind = '') {
  const d = el('sheet');
  d.className = 'wz-sheet hl-sheet' + (kind ? ' hl-sheet-' + kind : '');
  const body = el('sheet-body');
  const h2 = h('h2', '', title); h2.id = 'sheet-h';
  body.replaceChildren(h2, ...nodes);
  if (window.WzSheet) WzSheet.open(d); else d.showModal();
}
const ABOUT = {
  recovery: { title: 'Recovery', text: 'How ready your body is, from your heart rate variability, resting heart rate and sleep.',
    get: recov, show: v => Math.round(v) + '%', lo: 0, hi: 100,
    note: 'High is 67% and up, Medium 34\u201366%, Low 33% and under.' },
  sleep: { title: 'Sleep', text: 'Time actually asleep, next to the sleep your body needed that night.',
    get: sleepH, show: hm, lo: 0 },
  activity: { title: 'Activity', text: 'How hard your heart worked all day, workouts and everyday moving together.',
    get: act, show: v => v.toFixed(1), lo: 0, hi: d => actMax(d),
    note: 'From 0 to 21: Light under 10, Moderate 10\u201314, High 14\u201318, All out 18 and up.' },
};
// The 30 days before a date, averaged: "your usual".
function usual(get, iso) { const xs = []; for (let i = 1; i <= 30; i++) xs.push(get(rec4(addDays(iso, -i)))); return mean(xs); }
function vsUsual(v, u, fmt, close) {
  if (!num(v) || !num(u)) return '';
  const word = Math.abs(v - u) <= close ? 'about your usual' : v > u ? 'above your usual' : 'below your usual';
  return word + ' of ' + fmt(u);
}
function rows(list) {
  const dl = h('dl', 'hl-kv');
  for (const [k, v, sub] of list) {
    if (v == null || v === '') continue;
    const r = h('div'); r.append(h('dt', '', k));
    const dd = h('dd'); dd.append(h('b', '', v)); if (sub) dd.append(h('small', '', sub)); r.append(dd); dl.append(r);
  }
  return dl.children.length ? dl : null;
}
// Sleep stages as one bar: deep, REM, light and awake, in the order the eye reads them.
function stages(sl) {
  const parts = [['deep', 'Deep', sl.deep_h], ['rem', 'REM', sl.rem_h], ['light', 'Light', sl.light_h], ['awake', 'Awake', sl.awake_h]].filter(p => num(p[2]) && p[2] > 0);
  const total = parts.reduce((t, p) => t + p[2], 0);
  if (!total) return null;
  const box = h('div', 'hl-stages');
  const bar = h('div', 'hl-stbar'); bar.setAttribute('role', 'img');
  bar.setAttribute('aria-label', 'Sleep stages: ' + parts.map(p => p[1] + ' ' + dur(p[2])).join(', '));
  const key = h('ul', 'hl-stkey');
  for (const [k, name, v] of parts) {
    const i = h('i', 'st-' + k); i.style.flexGrow = String(v); bar.append(i);
    const li = h('li'); li.append(h('i', 'st-' + k), h('span', '', name), h('b', '', dur(v))); key.append(li);
  }
  box.append(bar, key);
  return box;
}
function openAbout(k) {
  const A = ABOUT[k], iso = shown(), d = rec4(iso) || {}, F = focusOf(k, d, !!sel);
  const now = h('div', 'hl-now');
  now.append(h('b', '', F.value));
  if (F.of) now.append(h('small', '', F.of));
  if (F.chip) { const c = h('span'); setChip(c, F); now.append(c); }
  const nodes = [h('p', 'hl-when', dayName(iso)), now, h('p', 'hl-about', A.text)];
  if (k === 'recovery') {
    const r = (fn, f, c) => [fn(d), vsUsual(fn(d), usual(fn, iso), f, c)];
    const [hv, hu] = r(hrv, v => Math.round(v) + ' ms', 3), [rh, ru] = r(x => (x || {}).resting_hr_bpm, v => Math.round(v) + ' bpm', 1);
    const tc = x => (x || {}).skin_temp_c, [tv, tu] = r(tc, v => v.toFixed(1) + ' \u00b0C', 0.2);
    nodes.push(rows([
      ['HRV', num(hv) ? Math.round(hv) + ' ms' : null, hu],
      ['Resting heart rate', num(rh) ? Math.round(rh) + ' bpm' : null, ru],
      ['Blood oxygen', num(d.spo2_pct) ? d.spo2_pct.toFixed(1) + '%' : null, ''],
      ['Skin temperature', num(tv) ? tv.toFixed(1) + ' \u00b0C' : null, tu],
    ]));
  }
  if (k === 'sleep') {
    const sl = d.sleep || {};
    nodes.push(stages(sl), rows([
      ['Time in bed', num(sl.in_bed_h) ? hm(sl.in_bed_h) : null, ''],
      ['Sleep performance', num(sl.score) ? Math.round(sl.score) + '%' : null, ''],
      ['Efficiency', num(sl.efficiency_pct) ? Math.round(sl.efficiency_pct) + '%' : null, 'time in bed spent asleep'],
    ]));
  }
  if (k === 'activity') {
    nodes.push(rows([
      ['Calories', num(d.calories_kcal) ? Math.round(d.calories_kcal).toLocaleString('en-US') + ' kcal' : null, ''],
      ['Steps', num(d.steps) ? Math.round(d.steps).toLocaleString('en-US') : null, ''],
      ['Average heart rate', num(d.avg_hr_bpm) ? Math.round(d.avg_hr_bpm) + ' bpm' : null, ''],
    ]));
    if ((d.workouts || []).length) {
      const ul = h('ul', 'hl-work');
      for (const w of d.workouts) {
        const li = h('li');
        li.append(h('b', '', cap(w.sport) || 'Workout'), h('span', '', [num(w.minutes) ? w.minutes + ' min' : '', num(w.calories_kcal) ? Math.round(w.calories_kcal) + ' kcal' : '', num((w.load || {}).value) ? 'activity ' + w.load.value.toFixed(1) : ''].filter(Boolean).join(' \u00b7 ')));
        ul.append(li);
      }
      nodes.push(ul);
    } else nodes.push(h('p', 'hl-note', 'No workouts this day.'));
  }
  nodes.push(chart(A, iso));
  if (A.note) nodes.push(h('p', 'hl-note', A.note));
  openSheet(A.title, nodes.filter(Boolean), 'about');
}
// A plain 30-day line ending on the shown day, with the average as a dashed line.
function chart(A, endIso) {
  const W = 320, H = 120, L = 6, R = 6, T = 10, B = 22, n = 30;
  const pts = [];
  for (let i = n - 1; i >= 0; i--) { const iso = addDays(endIso, -i); const v = A.get(rec4(iso)); pts.push({ iso, v }); }
  const vals = pts.map(p => p.v).filter(num);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'hl-chart', role: 'img', 'aria-label': `Last 30 days of ${A.title}` });
  if (!vals.length) return h('p', 'hl-key', 'No data in the last 30 days.');
  let lo = A.lo != null ? A.lo : Math.min(...vals), hi = typeof A.hi === 'function' ? A.hi(rec4(endIso)) : A.hi != null ? A.hi : Math.max(...vals);
  if (A.lo == null) { const pad = (hi - lo) * 0.2 || 4; lo -= pad; hi += pad; } else if (A.hi == null) hi *= 1.12;
  if (hi === lo) hi = lo + 1;
  const x = i => L + i * (W - L - R) / (n - 1);
  const y = v => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const avg = mean(vals);
  svg.append(s('line', { x1: L, x2: W - R, y1: H - B, y2: H - B, class: 'hl-axis' }));
  if (num(avg)) svg.append(s('line', { x1: L, x2: W - R, y1: y(avg).toFixed(1), y2: y(avg).toFixed(1), class: 'hl-avg' }));
  let d = '';
  pts.forEach((p, i) => { if (num(p.v)) d += (d ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p.v).toFixed(1); });
  svg.append(s('path', { d, class: 'hl-line' }));
  const lp = pts[n - 1];
  if (num(lp.v)) svg.append(s('circle', { cx: x(n - 1).toFixed(1), cy: y(lp.v).toFixed(1), r: 4, class: 'hl-dot' }));
  const t1 = s('text', { x: L, y: H - 6, class: 'hl-tx' }); t1.textContent = short(pts[0].iso);
  const t2 = s('text', { x: W - R, y: H - 6, class: 'hl-tx', 'text-anchor': 'end' }); t2.textContent = short(endIso);
  svg.append(t1, t2);
  const box = h('div', 'hl-chartbox');
  box.append(h('p', 'hl-ch', 'Last 30 days'), svg);
  if (num(avg)) box.append(h('p', 'hl-key', 'Dashed line: your average, ' + A.show(avg) + '.'));
  return box;
}
function openSources() {
  const nodes = [];
  const list = h('ul', 'hl-srcs');
  const several = ST.sources.filter(x => x.days > 0).length > 1;
  for (const x of ST.sources) {
    const li = h('li', 'hl-src');
    const [label] = STATUS[x.status] || ['Unknown'];
    const ok = fresh(x);
    li.append(h('i', 'hl-sdot ' + (ok ? 'ok' : 'stale')));
    const m = h('span', 'hl-src-main');
    m.append(h('b', '', x.name + (x.account ? ' \u00b7 ' + x.account : '')),
      h('small', '', live(x) ? 'Synced ' + ago(x.last_sync) : label));
    if (x.status === 'needs_login') m.append(h('small', 'hl-warn', 'Sign in again in ' + x.name));
    else if (x.status === 'needs_update') m.append(h('small', 'hl-warn', 'Update ' + x.name + ' or Health'));
    else if (x.error || x.pull_error) m.append(h('small', 'hl-warn', x.error || x.pull_error));
    li.append(m);
    const a = h('span', 'hl-src-act');
    if (several && !x.primary) { const b = h('button', 'wz-button secondary hl-sm', 'Show'); b.type = 'button'; b.onclick = () => setPrimary(x.id); a.append(b); }
    if (x.installed) a.append(appLink('Open', x.id, x.app_path, 'wz-button secondary hl-sm'));
    else { const b = h('button', 'wz-button secondary hl-sm', 'Remove'); b.type = 'button'; b.onclick = () => forget(x.id); a.append(b); }
    li.append(a);
    list.append(li);
  }
  const more = ST.connectors.filter(c => !ST.sources.some(x => x.id === c.id && x.installed));
  for (const c of more) {
    const li = h('li', 'hl-src');
    li.append(h('i', 'hl-sdot off'));
    const m = h('span', 'hl-src-main'); m.append(h('b', '', c.name), h('small', '', c.installed ? 'Not connected' : 'In the App Store')); li.append(m);
    const a = h('span', 'hl-src-act');
    if (c.installed) a.append(appLink('Connect', c.id, c.app_path, 'wz-button hl-sm'));
    li.append(a);
    list.append(li);
  }
  nodes.push(list);
  const rb = h('button', 'wz-button secondary hl-refresh', 'Refresh'); rb.type = 'button'; rb.id = 'refresh';
  rb.onclick = async () => {
    rb.disabled = true; rb.textContent = 'Refreshing\u2026';
    try { ST = await api('api/refresh', {}); await loadData(); renderAll(); openSources(); say('Up to date.', 'note'); }
    catch (err) { say(err.message, 'error'); rb.disabled = false; rb.textContent = 'Refresh'; }
  };
  if (ST.sources.length) nodes.push(rb);
  openSheet('Sources', nodes, 'src');
}
async function setPrimary(id) {
  try { ST = await api('api/primary', { id }); sel = null; await loadData(); renderAll(); openSources(); }
  catch (err) { say(err.message, 'error'); }
}
async function forget(id) {
  try { ST = await api('api/forget', { id }); renderAll(); openSources(); } catch (err) { say(err.message, 'error'); }
}

// ---------- interaction: taps only ----------
for (const b of el('metric').children) b.onclick = () => setMetric(b.dataset.m);
el('fmore').onclick = () => openAbout(METRIC);
el('srcdot').onclick = openSources;
el('today').onclick = () => { pick(newest()); scrollToDay(newest()); };
el('cells').addEventListener('click', e => { const b = e.target.closest('button[data-d]'); if (b) { hoverIso = null; pick(b.dataset.d); } });
el('cells').addEventListener('pointerover', e => {
  if (e.pointerType !== 'mouse') return;
  const b = e.target.closest('button[data-d]'); hoverIso = b ? b.dataset.d : null; showTip();
});
el('cells').addEventListener('pointerleave', () => { if (hoverIso) { hoverIso = null; showTip(); } });
el('gscroll').addEventListener('scroll', () => { fitMonths(); showTip(); }, { passive: true });
document.addEventListener('keydown', e => {
  if (el('sheet').open || !hasData() || e.target.closest('input, textarea')) return;
  const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 }[e.key];
  if (!step || e.target.closest('#metric')) return;
  const span = e.key === 'ArrowLeft' || e.key === 'ArrowRight' ? 7 : 1;
  const iso = addDays(shown(), step * span);
  if (iso <= newest() && iso >= G.start) { e.preventDefault(); pick(iso); scrollToDay(iso); const b = el('cells').querySelector(`[data-d="${iso}"]`); if (b) b.focus({ preventScroll: true }); }
});
new ResizeObserver(() => { if (hasData() && newest()) renderGraph(); }).observe(el('main'));

// ---------- loading (cached copy first, then fresh) ----------
const CACHE = 'wizard-health.screen.v5', PREF = 'wizard-health.metric';
function saveCache() { try { localStorage.setItem(CACHE, JSON.stringify({ ST, SUM, DAYS })); } catch (e) {} }
function loadCache() {
  try { const m = localStorage.getItem(PREF); if (SCALE[m]) METRIC = m; } catch (e) {}
  try { const c = JSON.parse(localStorage.getItem(CACHE) || 'null'); if (c && c.ST) { ST = c.ST; SUM = c.SUM; setDays(c.DAYS || []); return true; } } catch (e) {}
  return false;
}
function setDays(days) { DAYS = days; BY = {}; for (const d of days) BY[d.date] = d; }
async function loadData() {
  if (!hasData()) { SUM = null; setDays([]); return; }
  const p = primary(), to = p && p.latest_date;
  const q = to ? `&from=${addDays(to, -399)}&to=${to}` : '';
  const [sum, days] = await Promise.all([api('api/summary'), api('api/days?limit=400' + q)]);
  SUM = sum; setDays(days.days || []);
  saveCache();
}
const sig = () => ST && JSON.stringify(ST.sources.map(x => [x.id, x.last_pull, x.days, x.primary, x.status]));
async function refresh() {
  try {
    const before = sig();
    ST = await api('api/status');
    if (before !== sig() || !SUM) await loadData(); else saveCache();
    renderAll();
  } catch (err) { say(err.message, 'error'); }
}
function poll() { clearTimeout(pollTimer); pollTimer = setTimeout(async () => { await refresh(); poll(); }, hasData() ? 60000 : 5000); }

// Domain Back: an open sheet closes first (WzSheet), then a past day returns to today, which is the root.
function canBack() { return hasData() && !!sel; }
function goBack() {
  if (!canBack()) return false;
  hoverIso = null; pick(newest()); scrollToDay(newest()); return true;
}
(function start() {
  if (!window.WizardApp || WizardApp.version !== 1) {
    say('Wizard system functions are unavailable. Open Health through Wizard.', 'error');
  } else {
    WizardApp.ready({ name: 'wizard-health', version: '0.5.0' });
    WizardApp.back.register({ canGoBack: canBack, back: goBack });
  }
  if (loadCache()) renderAll();
  refresh().then(poll);
})();
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
