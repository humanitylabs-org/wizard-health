'use strict';
// Health: Recovery, Sleep and Activity for the selected day, each in one plain sentence over its own GitHub-style
// contribution graph of every day (all three side by side in a wide window, one at a time in a narrow one), read
// from the box's health-data connectors.
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
const { num, recBand, REC_WORD, sleepBand, SLEEP_WORD, actWord, REC_SAY, ACT_SAY, ask, recoveryVsSleep } = window.HealthWords;
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
// Bands and words live in words.js (Recovery 0-33/34-66/67-100; Activity under 10, 10-14, 14-18, 18 and over).
const actLevel = v => !num(v) ? 0 : v >= 18 ? 5 : v >= 14 ? 4 : v >= 10 ? 3 : v >= 5 ? 2 : 1;
const sleepLevel = v => !num(v) ? 0 : v >= 8 ? 5 : v >= 7 ? 4 : v >= 6 ? 3 : v >= 5 ? 2 : 1;

// What each square means for each metric: its class, its tooltip words and the legend.
const SCALE = {
  recovery: {
    name: 'Recovery',
    cls: d => { const b = recBand(recov(d)); return b ? 'c-' + b : ''; },
    tip: d => num(recov(d)) ? 'Recovery ' + Math.round(recov(d)) + '%, ' + REC_WORD[recBand(recov(d))].toLowerCase() : null,
    cap: () => 'Each square is one day. Green = ready, yellow = so-so, red = run down.',
    ends: ['Run down', 'Ready'],
    keys: [['c-lo', 'Run down, 0\u201333%'], ['c-md', 'So-so, 34\u201366%'], ['c-hi', 'Ready, 67\u2013100%']],
  },
  sleep: {
    name: 'Sleep',
    cls: d => { const l = sleepLevel(sleepH(d)); return l ? 'c-g c-' + l : ''; },
    tip: d => num(sleepH(d)) ? 'Sleep ' + hm(sleepH(d)) : null,
    cap: dark => 'Each square is one night. ' + (dark ? 'Brighter' : 'Darker') + ' = more sleep.',
    ends: ['Less sleep', 'More sleep'],
    keys: [['c-g c-1', 'Under 5 hours'], ['c-g c-2', '5 to 6 hours'], ['c-g c-3', '6 to 7 hours'], ['c-g c-4', '7 to 8 hours'], ['c-g c-5', '8 hours or more']],
  },
  activity: {
    name: 'Activity',
    cls: d => { const l = actLevel(act(d)); return l ? 'c-a c-' + l : ''; },
    tip: d => num(act(d)) ? 'Activity ' + act(d).toFixed(1) + ' out of ' + actMax(d) : null,
    cap: dark => 'Each square is one day. ' + (dark ? 'Brighter' : 'Darker') + ' = harder day.',
    ends: ['Easier', 'Harder'],
    keys: [['c-a c-1', 'Light, under 5'], ['c-a c-2', 'Light, 5 to 10'], ['c-a c-3', 'Moderate, 10 to 14'], ['c-a c-4', 'High, 14 to 18'], ['c-a c-5', 'All out, 18 or more']],
  },
};

// ---------- the focus card: one metric, one day, one plain sentence ----------
// A duration in words-light form: 4h 23m, or 40m under an hour.
const dur = hours => { const m = Math.round(hours * 60); return m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm'; };
// Everything the focus card shows for one metric on one day, generated from the values.
function focusOf(metric, d, past) {
  if (metric === 'recovery') {
    const r = recov(d), b = recBand(r);
    if (!b) return { value: DASH, say: 'Not scored yet. Your wearable scores recovery after you wake up.', more: 'Why?' };
    return { value: Math.round(r) + '%', chip: REC_WORD[b], chipCls: 'c-' + b, say: REC_SAY[past ? 'past' : 'now'][b], more: 'Why?',
      also: recoveryVsSleep(r, sleepH(d), needH(d), past) };
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
  return { value: a.toFixed(1), of: 'out of ' + actMax(d), scale: '0 = resting all day, ' + actMax(d) + ' = all out', chip: w, chipCls: 'c-a c-' + Math.max(2, actLevel(a)), say: ACT_SAY[w], more: 'More' };
}

// ---------- the screen ----------
// Three panels, one per metric, each with its question, focus card and graph. A wide window shows all three side by side
// (container width, never the device); a narrow one shows the picked metric and puts the picker in a bottom bar.
const METRICS = ['recovery', 'sleep', 'activity'];
const WIDE = 680;                                    // keep in step with the @container rules in styles.css
const app = document.querySelector('.wz-app');
const wide = () => app.clientWidth >= WIDE;
const P = {};                                        // metric -> { root, q(cls) }
for (const root of document.querySelectorAll('.hl-panel')) {
  root.append(el('panel-tpl').content.cloneNode(true));
  const m = root.dataset.m, refs = {}, q = c => refs[c] || (refs[c] = root.querySelector('.' + c));
  P[m] = { m, root, q, cells: q('hl-cells'), months: q('hl-months'), sc: q('hl-gscroll'), gin: q('hl-gin'), graph: q('hl-graph'), tip: q('hl-tip'), G: {} };
}
// Panels the window shows right now (hidden ones have no width to size a graph by).
const visible = () => wide() ? METRICS.map(m => P[m]) : [P[METRIC]];

function renderAll() {
  const data = hasData();
  el('empty').hidden = data;
  el('screen').hidden = !data;
  el('dock').hidden = !data;
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
  el('date').textContent = dayName(iso);
  el('today').hidden = !sel;
  for (const m of METRICS) focusPanel(P[m], iso);
  for (const b of el('metric').children) b.setAttribute('aria-checked', String(b.dataset.m === METRIC));
  for (const m of METRICS) P[m].root.classList.toggle('is-on', m === METRIC);
}
function focusPanel(X, iso) {
  const q = X.q, m = X.m;
  const F = focusOf(m, rec4(iso) || {}, !!sel);
  q('fday').textContent = dayName(iso);
  q('ftoday').hidden = !sel;
  q('fnum').textContent = F.value;
  q('fof').textContent = F.of || '';
  setChip(q('fchip'), F);
  const bar = q('fbar');
  bar.hidden = F.bar == null;
  if (F.bar != null) { bar.className = 'hl-bar fbar ' + (F.chipCls || ''); bar.firstChild.style.width = (F.bar * 100).toFixed(1) + '%'; }
  q('fscale').textContent = F.scale || '';
  q('fscale').hidden = !F.scale;
  q('fsay').textContent = F.say;
  q('falso').textContent = F.also || '';
  q('falso').hidden = !F.also;
  const A = ask(m, !!sel);
  q('ask1').textContent = A.q;
  q('ask2').textContent = A.sub;
  q('ask2').hidden = !A.sub;
  const more = q('fmore');
  more.textContent = F.more;
  more.setAttribute('aria-label', (F.more === 'Why?' ? 'Why? ' : 'More about ') + SCALE[m].name.toLowerCase());
  q('hl-focus').setAttribute('aria-label', `${SCALE[m].name}, ${dayName(iso)}: ${F.value}${F.of ? ' ' + F.of : ''}${F.chip ? ', ' + F.chip : ''}. ${F.say}${F.also ? ' ' + F.also : ''}`);
}

// ---------- the graphs ----------
// One week span for all three graphs, so the same day sits in the same square in each.
let G = { start: null, weeks: 0, end: null };
function span() {
  const last = newest(); if (!last) return null;
  const today = localIso(new Date());
  const endDay = last > today ? last : today;
  const end = addDays(endDay, 6 - dow(endDay));                  // the Saturday closing the newest week
  const dates = DAYS.map(d => d.date).sort();
  const first = dates[0] && dates[0] < last ? dates[0] : last;
  // Only the weeks that hold data plus the current week (no grey lead-in); a history wider than the column scrolls
  // sideways, opening on today.
  const weeks = Math.max(1, Math.round((Date.parse(end) - Date.parse(addDays(first, -dow(first)))) / DAY + 1) / 7);
  return { start: addDays(end, -(weeks * 7 - 1)), weeks, end, endDay };
}
function renderGraph() {
  const g = span(); if (!g) return;
  const changed = G.start !== g.start || G.weeks !== g.weeks;
  G = g;
  for (const X of visible()) graphPanel(X, changed);
}
function buildCells(X) {
  const { cells, months } = X;
  quiet(150);
  cells.replaceChildren(); months.replaceChildren();
  let lastMonthCol = -9;
  for (let i = 0; i < G.weeks * 7; i++) {
    const iso = addDays(G.start, i);
    if (iso > G.endDay) { cells.append(h('i', 'hl-cell hl-future')); continue; }
    const b = h('button', 'hl-cell'); b.type = 'button'; b.dataset.d = iso;
    cells.append(b);
    // Month names sit over the first week that starts in that month, as on GitHub.
    if (i % 7 === 0) {
      const col = i / 7, wkMonth = iso.slice(0, 7), prev = col ? addDays(iso, -7).slice(0, 7) : null;
      if ((col === 0 && iso.slice(8) <= '24') || (col > 0 && wkMonth !== prev)) {
        if (col - lastMonthCol >= 2) { const mo = h('span', '', noon(iso).toLocaleDateString('en-US', { month: 'short' })); mo.style.setProperty('--col', col); months.append(mo); lastMonthCol = col; }
      }
    }
  }
  X.built = G.start + '/' + G.weeks;
}
function graphPanel(X, changed) {
  const rebuilt = X.built !== G.start + '/' + G.weeks;
  if (rebuilt) buildCells(X);
  const S = SCALE[X.m], on = shown();
  let tracked = 0;
  for (const b of X.cells.children) {
    const iso = b.dataset.d; if (!iso) continue;
    const d = rec4(iso), tip = d && S.tip(d);
    if (d) tracked++;
    b.className = 'hl-cell ' + (d ? S.cls(d) : '') + (iso === on ? ' on' : '');
    b.setAttribute('aria-label', short(iso) + ': ' + (tip || 'no data'));
    b.setAttribute('aria-pressed', String(iso === on));
  }
  renderLegend(X);
  const scrolls = sizeGraph(X);
  const cap = S.cap(darkScheme());
  X.q('hl-cap').textContent = cap;
  X.graph.setAttribute('aria-label', `${S.name}. ${cap} ${tracked} ${tracked === 1 ? 'day' : 'days'} tracked${scrolls ? ', older weeks to the left' : ''}.`);
  if (rebuilt || changed || X.cell !== X.lastCell) scrollToDay(X, on, true);
  X.lastCell = X.cell;
  fitMonths(X);
  showTip(X);
}
function renderLegend(X) {
  const S = SCALE[X.m], box = X.q('hl-legend');
  box.replaceChildren(h('span', '', S.ends[0]));
  for (const [cls, title] of S.keys) { const i = h('i', 'hl-cell ' + cls); i.title = title; box.append(i); }
  box.append(h('span', '', S.ends[1]));
  box.setAttribute('aria-label', S.name + ': ' + S.keys.map(k => k[1]).join(', '));
}
// Light skins draw a fuller day darker, dark skins brighter: the caption follows the scheme the shared client sets.
function darkScheme() {
  const c = document.documentElement.dataset.colorScheme;
  return c ? c === 'dark' : !!(window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
}
new MutationObserver(() => { if (hasData() && newest()) for (const m of METRICS) P[m].q('hl-cap').textContent = SCALE[m].cap(darkScheme()); })
  .observe(document.documentElement, { attributes: true, attributeFilter: ['data-color-scheme'] });
// Square size from the panel's width (the scroller is as wide as its card, never as wide as its content):
// every week fits when it can, in squares as big as fit; otherwise whole weeks of thumb-sized squares (22 px or more
// in a narrow window) fill the view and older weeks scroll sideways.
const GAP = 3;
function sizeGraph(X) {
  const sc = X.sc, avail = sc.clientWidth - 4;                   // .hl-gin's side padding
  if (avail <= 0 || !G.weeks) return false;
  const narrow = !wide(), min = narrow ? 22 : 15, max = narrow ? 36 : 34;
  let cell = Math.floor((avail + GAP) / G.weeks) - GAP, vis = G.weeks;
  if (cell < min) { vis = Math.max(1, Math.floor((avail + GAP) / (min + GAP))); cell = Math.floor((avail + GAP) / vis) - GAP; }
  cell = Math.max(10, Math.min(cell, max));
  const scrolls = G.weeks > vis;
  X.vis = vis; X.step = cell + GAP; X.cell = cell;
  X.graph.style.setProperty('--cell', cell + 'px');
  X.graph.style.setProperty('--gap', GAP + 'px');
  // When scrolling, the rounding remainder pads the right end so whole weeks line up with both edges (no sliver).
  X.gin.style.paddingRight = scrolls ? Math.max(2, avail - (vis * X.step - GAP) + 2) + 'px' : '';
  X.graph.classList.toggle('hl-scrolls', scrolls);
  return scrolls;
}
// A month name cut by either edge of the visible squares, or crowding the next one, is hidden (no stray "un").
function fitMonths(X) {
  const sc = X.sc, L = sc.scrollLeft, R = L + sc.clientWidth, W = X.cells.offsetWidth + 2;
  let next = Infinity;
  for (const mo of [...X.months.children].reverse()) {
    const x = mo.offsetLeft, w = mo.offsetWidth;
    const hide = x < L - 1 || x + w > Math.min(R, W + 2) + 1 || x + w + 6 > next;
    mo.style.visibility = hide ? 'hidden' : '';
    if (!hide) next = x;
  }
}
// Bring a day's week into view, keeping whole weeks at both edges.
function scrollToDay(X, iso, instant) {
  const sc = X.sc, b = iso && X.cells.querySelector(`[data-d="${iso}"]`);
  if (!b || !X.step) return;
  const col = Math.round((b.offsetLeft - 2) / X.step), first = Math.round(sc.scrollLeft / X.step), vis = X.vis || G.weeks;
  let left = null;
  if (col < first) left = col;
  else if (col > first + vis - 1) left = col - vis + 1;
  if (instant && col >= G.weeks - 1) left = Math.max(0, G.weeks - vis);   // opening on today: the newest weeks, flush
  if (left != null) { quiet(instant ? 150 : 700); sc.scrollTo({ left: Math.max(0, left * X.step), behavior: instant ? 'instant' : 'smooth' }); }
}
const scrollAll = (iso, instant) => { for (const X of visible()) scrollToDay(X, iso, instant); };
// Side by side, the three graphs scroll together when the owner scrolls one, so a week sits under the same week in every
// column. The app's own scrolls (opening on today, picking a day) move all three alike and are not echoed.
let quietUntil = 0, following = false;
const quiet = ms => { quietUntil = Math.max(quietUntil, performance.now() + ms); };
function syncScroll(src) {
  if (following || !wide() || performance.now() < quietUntil) return;
  following = true;
  for (const m of METRICS) { const X = P[m]; if (X !== src && Math.abs(X.sc.scrollLeft - src.sc.scrollLeft) > 1) X.sc.scrollLeft = src.sc.scrollLeft; }
  requestAnimationFrame(() => requestAnimationFrame(() => { following = false; }));
}
// GitHub-style tooltip over the square under the mouse (taps show the day in the focus cards instead).
let hoverM = null;
function showTip(X) {
  const tip = X.tip;
  const iso = hoverM === X.m ? hoverIso : null;
  const b = iso && X.cells.querySelector(`[data-d="${iso}"]`);
  if (!b) { tip.hidden = true; return; }
  const d = rec4(iso), t = d && SCALE[X.m].tip(d);
  tip.textContent = short(iso) + ' \u00b7 ' + (t || 'No data');
  tip.hidden = false;
  const g = X.graph.getBoundingClientRect(), r = b.getBoundingClientRect(), sc = X.sc.getBoundingClientRect();
  if (r.right < sc.left || r.left > sc.right) { tip.hidden = true; return; }
  const tw = tip.offsetWidth;
  let x = r.left + r.width / 2 - g.left - tw / 2;
  x = Math.max(4, Math.min(g.width - tw - 4, x));
  tip.style.left = x + 'px';
  tip.style.top = (r.top - g.top - tip.offsetHeight - 6) + 'px';
  tip.style.setProperty('--ax', (r.left + r.width / 2 - g.left - x) + 'px');
}
// A day picked in any graph is the day every card and graph shows.
function pick(iso) {
  const n = newest();
  if (!iso || !n || iso > n) return;
  sel = iso === n ? null : iso;
  renderFocus(); renderGraph();
}
// Switching metric (narrow windows) keeps the selected day.
function setMetric(m) {
  if (!SCALE[m] || m === METRIC) return;
  METRIC = m;
  try { localStorage.setItem(PREF, m); } catch (e) {}
  renderFocus(); renderGraph();
  if (!wide()) el('main').scrollTop = 0;
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
  recovery: { title: 'Recovery', text: 'How ready your body is. Your wearable reads your heart while you sleep, so this can differ from how long you slept.',
    get: recov, show: v => Math.round(v) + '%', lo: 0, hi: 100,
    note: 'Ready is 67% and up, So-so 34\u201366%, Run down 33% and under.' },
  sleep: { title: 'Sleep', text: 'Time actually asleep, next to the sleep your body needed that night.',
    get: sleepH, show: hm, lo: 0 },
  activity: { title: 'Activity', text: 'How hard your heart worked all day, workouts and everyday moving together. 0 = resting all day, 21 = all out.',
    get: act, show: v => v.toFixed(1), lo: 0, hi: d => actMax(d),
    note: 'Light under 10, Moderate 10\u201314, High 14\u201318, All out 18 and up.' },
};
// The 30 days before a date, averaged: "your usual".
function usual(get, iso) { const xs = []; for (let i = 1; i <= 30; i++) xs.push(get(rec4(addDays(iso, -i)))); return mean(xs); }
function vsUsual(v, u, fmt, close) {
  if (!num(v) || !num(u)) return '';
  // Short enough to sit beside the row's gloss on a phone; the two numbers show above/below by themselves.
  return (Math.abs(v - u) <= close ? 'about usual, ' : 'usual ') + fmt(u);
}
function rows(list) {
  const dl = h('dl', 'hl-kv');
  for (const [k, v, sub, gloss] of list) {
    if (v == null || v === '') continue;
    const r = h('div'), dt = h('dt');
    dt.append(h('span', '', k));
    if (gloss) dt.append(h('small', '', gloss));
    r.append(dt);
    const dd = h('dd'); dd.append(h('b', '', v)); if (sub) dd.append(h('small', '', sub)); r.append(dd); dl.append(r);
  }
  return dl.children.length ? dl : null;
}
// Sleep stages as one bar: deep, REM, light and awake, in the order the eye reads them.
function stages(sl) {
  const parts = [['deep', 'Deep', sl.deep_h], ['rem', 'Dreaming', sl.rem_h], ['light', 'Light', sl.light_h], ['awake', 'Awake', sl.awake_h]].filter(p => num(p[2]) && p[2] > 0);
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
      ['Heart rate variability (HRV)', num(hv) ? Math.round(hv) + ' ms' : null, hu, 'Higher usually means more recovered'],
      ['Resting heart rate', num(rh) ? Math.round(rh) + ' bpm' : null, ru, 'Lower is usually better'],
      ['Blood oxygen', num(d.spo2_pct) ? d.spo2_pct.toFixed(1) + '%' : null, '', 'Oxygen in your blood overnight'],
      ['Skin temperature', num(tv) ? tv.toFixed(1) + ' \u00b0C' : null, tu, 'A rise can be an early sign of illness'],
    ]));
  }
  if (k === 'sleep') {
    const sl = d.sleep || {};
    nodes.push(stages(sl), rows([
      ['Time in bed', num(sl.in_bed_h) ? hm(sl.in_bed_h) : null, '', 'From lying down to getting up'],
      ['Sleep vs need', num(sl.score) ? Math.round(sl.score) + '%' : null, '', 'How much of the sleep you needed you got'],
      ['Asleep while in bed', num(sl.efficiency_pct) ? Math.round(sl.efficiency_pct) + '%' : null, '', 'Higher means less tossing and turning'],
    ]));
  }
  if (k === 'activity') {
    nodes.push(rows([
      ['Calories', num(d.calories_kcal) ? Math.round(d.calories_kcal).toLocaleString('en-US') + ' kcal' : null, '', 'Energy your body burned all day'],
      ['Steps', num(d.steps) ? Math.round(d.steps).toLocaleString('en-US') : null, '', 'Counted by your wearable'],
      ['Average heart rate', num(d.avg_hr_bpm) ? Math.round(d.avg_hr_bpm) + ' bpm' : null, '', 'Across the whole day'],
    ]));
    if ((d.workouts || []).length) {
      const ul = h('ul', 'hl-work');
      for (const w of d.workouts) {
        const li = h('li');
        li.append(h('b', '', cap(w.sport) || 'Workout'), h('span', '', [num(w.minutes) ? w.minutes + ' min' : '', num(w.calories_kcal) ? Math.round(w.calories_kcal) + ' kcal' : '', num((w.load || {}).value) ? 'effort ' + w.load.value.toFixed(1) + ' of ' + (w.load.max || actMax(d)) : ''].filter(Boolean).join(' \u00b7 ')));
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
el('srcdot').onclick = openSources;
const toToday = () => { hoverIso = null; pick(newest()); scrollAll(newest()); };
el('today').onclick = toToday;
for (const m of METRICS) {
  const X = P[m];
  X.q('fmore').onclick = () => openAbout(m);
  X.q('ftoday').onclick = toToday;
  X.cells.addEventListener('click', e => { const b = e.target.closest('button[data-d]'); if (b) { hoverIso = null; pick(b.dataset.d); } });
  X.cells.addEventListener('pointerover', e => {
    if (e.pointerType !== 'mouse') return;
    const b = e.target.closest('button[data-d]'); hoverIso = b ? b.dataset.d : null; hoverM = m; showTip(X);
  });
  X.cells.addEventListener('pointerleave', () => { if (hoverIso) { hoverIso = null; showTip(X); } });
  X.sc.addEventListener('scroll', () => { fitMonths(X); showTip(X); syncScroll(X); }, { passive: true });
}
document.addEventListener('keydown', e => {
  if (el('sheet').open || !hasData() || e.target.closest('input, textarea')) return;
  const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 }[e.key];
  if (!step || e.target.closest('#metric')) return;
  const span = e.key === 'ArrowLeft' || e.key === 'ArrowRight' ? 7 : 1;
  const iso = addDays(shown(), step * span);
  if (iso <= newest() && iso >= G.start) {
    e.preventDefault(); pick(iso); scrollAll(iso);
    const host = e.target.closest('.hl-panel') || P[METRIC].root, b = host.querySelector(`.hl-cells [data-d="${iso}"]`);
    if (b) b.focus({ preventScroll: true });
  }
});
let wasWide = null;
new ResizeObserver(() => {
  const w = wide();
  if (w !== wasWide) { wasWide = w; for (const m of METRICS) P[m].built = null; }
  if (hasData() && newest()) renderGraph();
}).observe(app);

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
  toToday(); return true;
}
(function start() {
  if (!window.WizardApp || WizardApp.version !== 1) {
    say('Wizard system functions are unavailable. Open Health through Wizard.', 'error');
  } else {
    WizardApp.ready({ name: 'wizard-health', version: '0.7.0' });
    WizardApp.back.register({ canGoBack: canBack, back: goBack });
  }
  if (loadCache()) renderAll();
  refresh().then(poll);
})();
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
