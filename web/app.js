'use strict';
// Health: Recovery, Sleep and Activity for the selected day. Each is a title, one summary card filled with the same
// colour as that day's square, its few supporting numbers baked in, and its own GitHub-style contribution graph of every
// day (all three side by side in a wide window, one at a time in a narrow one), read from the box's health-data connectors.
// Reads only this app's documented relative API; never talks to a provider and never takes keys.
const el = id => document.getElementById(id);
const SVG = 'http://www.w3.org/2000/svg';
const DAY = 86400000;
let ST = null, SUM = null, DAYS = [], BY = {}, pollTimer = null;
let sel = null;            // the selected date (ISO); null = the newest day ("today")
let METRIC = 'activity';   // narrow windows: the metric shown; always opens on Activity (what to do today)
let hoverIso = null;
let OPEN = false;          // narrow windows: the summary card's details are folded until the card is tapped

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
const { num, recBand, REC_WORD, sleepBand, SLEEP_WORD, actMatch, actGuide, matchSay, targetText, REC_SAY, title, sleepSay, needGloss, recoveryVsSleep, betterInk } = window.HealthWords;
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
  not_configured: ['Not set up', 'off'], not_installed: ['Not installed', 'off'], needs_update: ['Needs an update', 'warn'] };
// Fresh = the source is signed in and synced within the last 3 hours.
const fresh = src => live(src) && src.last_sync && Date.now() - new Date(src.last_sync) < 3 * 3600000;
// The Sources sheet's one word per source (the server's `state`; older servers only sent `status`).
const stateOf = x => x.state || (!x.installed ? 'not_installed' : x.status === 'needs_update' ? 'needs_update'
  : live(x) ? (fresh(x) ? 'connected' : 'stale') : x.status === 'not_configured' ? 'not_set_up' : x.status === 'needs_login' ? 'sign_in' : 'error');
const srcOf = id => (ST && ST.sources.find(x => x.id === id)) || null;
const srcName = id => (srcOf(id) || {}).name || (SUM && SUM.source_names && SUM.source_names[id]) || id;
// A metric another source filled because the main source had none that day: its tiny origin note.
function fromNote(d, key) {
  const o = d && d.origins && d.origins[key];
  return o && d.source && o !== d.source ? 'from ' + srcName(o) : '';
}
// No readiness score because no source of this day gives one: name it ("No score from Oura"); never invent a score.
function noScoreFrom(d) {
  if (num(recov(d)) || !d) return null;
  const used = [...new Set([d.source, ...Object.values(d.origins || {})].filter(Boolean))];
  if (!used.length || used.some(id => { const x = srcOf(id); return !x || x.has_readiness !== false; })) return null;
  return srcName(used[0]);
}

// ---------- the numbers, in plain words (one record shape: the summary or a stored day) ----------
const labels = () => (SUM && SUM.labels) || {};
const newest = () => (SUM && SUM.available && SUM.day) || (primary() || {}).latest_date || null;
const shown = () => sel || newest();
// The record for a date: the summary for its own day (it fills an unscored newest day from the last scored one).
const rec4 = iso => (SUM && SUM.available && SUM.day === iso) ? SUM : BY[iso] || null;
const recov = d => ((d || {}).readiness || {}).score;
const sleepH = d => ((d || {}).sleep || {}).asleep_h;
const needH = d => ((d || {}).sleep || {}).need_h;
// The usual night inside the need (the rest is catch-up for earlier short nights), when the source splits it.
const usualH = d => (((d || {}).sleep || {}).need_parts || {}).usual_h;
const act = d => ((d || {}).load || {}).value;
const loadOf = d => (d || {}).load || null;
// The source's own scale for the card: "out of 21" (WHOOP, a scale with a top), or what it counts ("active zone
// minutes") when the scale is open-ended (max null; its scale_max then only sets targets).
const actTop = d => { const L = loadOf(d) || {}; return num(L.max) ? L.max : L.max === null ? null : labels().load_max; };
const actOf = d => { const L = loadOf(d) || {}, mx = actTop(d); return num(mx) ? 'out of ' + mx : String(L.label || L.unit || '').toLowerCase(); };
// One decimal on small scales (WHOOP 0-21: 5.9); whole numbers stay whole on bigger ones.
const actFmt = (v, d) => { const L = loadOf(d) || {}, mx = num(actTop(d)) ? actTop(d) : L.scale_max;
  return num(mx) && mx > 30 ? (Number.isInteger(v) ? String(v) : v.toFixed(1)) : v.toFixed(1); };
const hrv = d => (d || {}).hrv_ms;
// Bands and words live in words.js: one traffic light for all three (green good, yellow okay, red needs attention).
// Recovery 67+/34-66/0-33; Sleep 85%+/70-85%/under 70% of need; Activity by how its effort matched that day's recovery.
const sleepPct = d => num(sleepH(d)) && num(needH(d)) && needH(d) > 0 ? Math.round(100 * sleepH(d) / needH(d)) : null;
const band = {
  recovery: d => recBand(recov(d)),
  sleep: d => sleepBand(sleepH(d), needH(d)),
  activity: d => (actMatch(act(d), recov(d), loadOf(d)) || {}).band || null,
};
// The one class every square and card wears: c-hi green, c-md yellow, c-lo red; no class = grey (no data).
const LIGHT = { hi: 'c-hi', md: 'c-md', lo: 'c-lo' };
const lightOf = (m, d) => LIGHT[band[m](d)] || '';

// What each square means for each metric: its class (the summary card wears the very same class, so the card is always
// the colour of the shown day's square) and its tooltip words. One legend for all three lives under the graphs.
const SCALE = {
  recovery: {
    name: 'Recovery',
    cls: d => lightOf('recovery', d),
    tip: d => num(recov(d)) ? 'Recovery ' + Math.round(recov(d)) + '%, ' + REC_WORD[recBand(recov(d))].toLowerCase() : null,
  },
  sleep: {
    name: 'Sleep',
    cls: d => lightOf('sleep', d),
    tip: d => !num(sleepH(d)) ? null : 'Sleep ' + hm(sleepH(d)) + (num(sleepPct(d)) ? ' (' + sleepPct(d) + '% of need)' : '') + ', ' + SLEEP_WORD[band.sleep(d)].toLowerCase(),
  },
  activity: {
    name: 'Activity',
    cls: d => lightOf('activity', d),
    tip: d => { if (!num(act(d))) return null; const mt = actMatch(act(d), recov(d), loadOf(d));
      return 'Activity ' + actFmt(act(d), d) + ', ' + (mt ? mt.word.toLowerCase() : num(recov(d)) ? 'no target' : 'no recovery data'); },
  },
};
const LEGEND = [['c-hi', 'Green', 'good'], ['c-md', 'Yellow', 'okay'], ['c-lo', 'Red', 'needs attention'], ['', '', 'No data']];

// ---------- the summary card: one metric, one day, one short summary, the key numbers ----------
// A duration in words-light form: 4h 23m, or 40m under an hour.
const dur = hours => { const m = Math.round(hours * 60); return m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm'; };
// Everything the card shows for one metric on one day, generated from the values.
function focusOf(metric, d, past) {
  if (metric === 'recovery') {
    const r = recov(d), b = recBand(r), none = noScoreFrom(d);
    if (!b && none) return { value: DASH, chip: 'No score from ' + none, say: none + ' does not give a recovery score.' };
    if (!b) return { value: DASH, say: 'Not scored yet. Your wearable scores it after you wake up.' };
    return { value: Math.round(r) + '%', chip: REC_WORD[b], say: REC_SAY[past ? 'past' : 'now'][b], also: recoveryVsSleep(r, sleepH(d), needH(d), past) };
  }
  if (metric === 'sleep') {
    const sh = sleepH(d), nd = needH(d), b = sleepBand(sh, nd);
    if (!num(sh)) return { value: DASH, say: 'No sleep recorded for this night.' };
    if (!num(nd) || nd <= 0) return { value: hm(sh), chip: SLEEP_WORD[b], say: 'You slept ' + dur(sh) + '.' };
    const um = num(usualH(d)) ? Math.round(usualH(d) * 60) : null;
    return { value: hm(sh), of: 'of ' + hm(nd) + ' needed', chip: SLEEP_WORD[b], say: sleepSay(Math.round(sh * 60), Math.round(nd * 60), um) };
  }
  const a = act(d);
  if (!num(a)) return { value: DASH, say: 'No activity recorded for this day.' };
  const mt = actMatch(a, recov(d), loadOf(d));
  if (!mt && !num(recov(d))) return { value: actFmt(a, d), of: actOf(d), chip: 'No recovery data', say: 'No recovery score, so no target for this day.' };
  if (!mt) return { value: actFmt(a, d), of: actOf(d), chip: 'No target', say: 'No scale from this source to set a target.' };
  // The direction in one line ("11.5 to go" / "1.5 over") and the bar that draws it: a yellow card says which way to go.
  const gd = actGuide(a, recov(d), loadOf(d), past);
  return { value: actFmt(a, d), of: actOf(d), chip: mt.word, say: gd ? gd.text : matchSay(mt, recov(d), past), guide: gd };
}
// The 30 days before a date, averaged: "your usual".
function usual(get, iso) { const xs = []; for (let i = 1; i <= 30; i++) xs.push(get(rec4(addDays(iso, -i)))); return mean(xs); }
function vsUsual(v, u, fmt, close) {
  if (!num(v) || !num(u)) return '';
  return Math.abs(v - u) <= close ? 'about usual' : 'usual ' + fmt(u);   // the unit is on the value above
}
// The supporting numbers, baked into the card: 2-4 rows of [name, value, short gloss, vs usual, stage swatch].
function detailsOf(metric, d, iso) {
  if (metric === 'recovery') {
    const r = (fn, f, c) => [fn(d), vsUsual(fn(d), usual(fn, iso), f, c)];
    const [hv, hu] = r(hrv, v => String(Math.round(v)), 3), [rh, ru] = r(x => (x || {}).resting_hr_bpm, v => String(Math.round(v)), 1);
    const [tv, tu] = r(x => (x || {}).skin_temp_c, v => v.toFixed(1) + '\u00b0', 0.2);
    return [
      ['Heart rate variability', num(hv) ? Math.round(hv) + ' ms' : null, 'higher = more recovered', hu, '', fromNote(d, 'hrv_ms')],
      ['Resting heart rate', num(rh) ? Math.round(rh) + ' bpm' : null, 'lower is better', ru, '', fromNote(d, 'resting_hr_bpm')],
      ['Blood oxygen', num(d.spo2_pct) ? d.spo2_pct.toFixed(1) + '%' : null, 'overnight', '', '', fromNote(d, 'spo2_pct')],
      ['Skin temperature', num(tv) ? tv.toFixed(1) + ' \u00b0C' : null, 'a rise can mean illness', tu, '', fromNote(d, 'skin_temp_c')],
    ];
  }
  if (metric === 'sleep') {
    const sl = d.sleep || {};
    const nd = needH(d), um = num(usualH(d)) ? Math.round(usualH(d) * 60) : null;
    return [
      ['Your need', num(nd) && nd > 0 ? hm(nd) : null, num(nd) ? needGloss(Math.round(nd * 60), um) : '', ''],
      ['Deep', num(sl.deep_h) ? dur(sl.deep_h) : null, 'body repair', '', 'deep'],
      ['Dreaming (REM)', num(sl.rem_h) ? dur(sl.rem_h) : null, 'mind and memory', '', 'rem'],
      ['Light', num(sl.light_h) ? dur(sl.light_h) : null, '', '', 'light'],
      ['Time in bed', num(sl.in_bed_h) ? hm(sl.in_bed_h) : null, num(sl.awake_h) && sl.awake_h > 0 ? dur(sl.awake_h) + ' awake' : '', ''],
    ];
  }
  const ws = d.workouts || [], rb = recBand(recov(d));
  const one = w => (cap(w.sport) || 'Workout') + (num(w.minutes) ? ', ' + w.minutes + '\u00a0min' : '');
  return [
    // the target row only when there is no bar (the bar already marks the target with its ticks)
    [sel ? 'Target that day' : 'Target for today', rb && !actGuide(act(d), recov(d), loadOf(d)) ? targetText(rb, loadOf(d)) || null : null, rb ? 'from your recovery' : '', ''],
    ['Calories', num(d.calories_kcal) ? Math.round(d.calories_kcal).toLocaleString('en-US') + ' kcal' : null, 'burned all day', '', '', fromNote(d, 'calories_kcal')],
    ['Steps', num(d.steps) ? Math.round(d.steps).toLocaleString('en-US') : null, '', '', '', fromNote(d, 'steps')],
    ['Workouts', !Object.keys(d).length || !Array.isArray(d.workouts) && d.source ? null : !ws.length ? 'None' : ws.length === 1 ? one(ws[0]) : ws.length + ': ' + ws.map(w => cap(w.sport) || 'Workout').join(', '), '', '', '', fromNote(d, 'workouts')],
  ];
}
// The card's headline metric came from another source: one tiny note at the end of the details.
const HEAD = { recovery: ['readiness', 'Score'], sleep: ['sleep', 'Sleep'], activity: ['load', 'Activity'] };
function headFrom(m, d) { const [key, what] = HEAD[m], f = fromNote(d, key); return f ? what + ' ' + f : ''; }
function fillRows(dl, list) {
  dl.replaceChildren();
  // [name, value, gloss, vs usual, stage swatch, origin note ("from Oura": another source filled this value)]
  for (const [k, v, gloss, sub, st, from] of list) {
    if (v == null || v === '') continue;
    const r = h('div'), dt = h('dt'), dd = h('dd'), name = h('span');
    if (st) name.append(h('i', 'st-' + st));
    name.append(k); dt.append(name);
    if (gloss) dt.append(h('small', '', gloss));
    dd.append(h('b', '', v)); if (sub) dd.append(h('small', '', sub));
    if (from) dd.append(h('small', 'hl-ofrom', from));
    r.append(dt, dd); dl.append(r);
  }
  dl.hidden = !dl.children.length;
}
// Activity's progress bar: the green target zone on the scale, a marker where the day's effort is now, the zone's edges
// as ticks. Drawn in the card's own text colour (the fill already carries the verdict), so it reads on every fill.
const pct = (x, max) => Math.max(0, Math.min(100, 100 * x / max)) + '%';
function fillBar(bar, g) {
  bar.hidden = !g;
  if (!g) return;
  const zone = bar.querySelector('.hl-azone'), mark = bar.querySelector('.hl-amark'), ticks = bar.querySelector('.hl-aticks');
  zone.style.left = pct(g.lo, g.max); zone.style.width = pct(g.hi - g.lo, g.max);
  mark.style.left = pct(g.v, g.max);
  const fmt = x => Number.isInteger(x) ? String(x) : x.toFixed(1);
  ticks.replaceChildren(...(g.lo > 0 ? [g.lo, g.hi] : [g.hi]).map(x => { const t = h('span', '', fmt(x)); t.style.left = pct(x, g.max); return t; }));
  bar.setAttribute('aria-label', `Target ${g.lo > 0 ? fmt(g.lo) + ' to ' : 'under '}${fmt(g.hi)}, now ${fmt(g.v)}`);
}
// Sleep stages as one thin bar: deep, dreaming, light and awake, in the order the eye reads them.
function fillStages(bar, sl) {
  const parts = [['deep', 'Deep', sl.deep_h], ['rem', 'Dreaming', sl.rem_h], ['light', 'Light', sl.light_h], ['awake', 'Awake', sl.awake_h]].filter(p => num(p[2]) && p[2] > 0);
  bar.replaceChildren();
  bar.hidden = !parts.length;
  if (!parts.length) return;
  for (const [k, , v] of parts) { const i = h('i', 'st-' + k); i.style.flexGrow = String(v); bar.append(i); }
  bar.setAttribute('aria-label', 'Sleep stages: ' + parts.map(p => p[1] + ' ' + dur(p[2])).join(', '));
}

// Text on the coloured card: the kit's ink or its canvas colour, whichever contrasts more with this very fill.
// Colours are normalised through a 1 px canvas (computed colour-mix values come back in several notations).
const CV = document.createElement('canvas'); CV.width = CV.height = 1;
const CX = CV.getContext('2d', { willReadFrequently: true });
function rgba(css) { CX.clearRect(0, 0, 1, 1); CX.fillStyle = css; CX.fillRect(0, 0, 1, 1); return [...CX.getImageData(0, 0, 1, 1).data]; }
const probe = h('i'); probe.style.cssText = 'position:absolute;width:0;height:0;visibility:hidden';
document.querySelector('.wz-app').append(probe);
function cssColor(v) { probe.style.color = v; return getComputedStyle(probe).color; }
function onTop(c, under) { const a = c[3] / 255; return [0, 1, 2].map(i => Math.round(c[i] * a + under[i] * (1 - a))); }
function inkFor(card) {
  const base = rgba(cssColor('var(--canvas)')).slice(0, 3);
  const fill = onTop(rgba(getComputedStyle(card).backgroundColor), base);
  return betterInk(fill, rgba(cssColor('var(--ink)')).slice(0, 3), base) === 'a' ? 'ink' : 'canvas';
}

// ---------- the screen ----------
// Three panels, one per metric, each with its question, focus card and graph. A wide window shows all three side by side
// (container width, never the device); a narrow one shows the picked metric and puts the picker in a bottom bar.
const METRICS = ['activity', 'recovery', 'sleep'];
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
  renderLegend();
}
// Nothing to show yet: one big button. With one connector app installed it opens it; with several, each is listed
// below as its own setup link; with none, the big button opens Sources (which names the App Store connectors).
function renderEmpty() {
  const apps = [...ST.sources.filter(x => x.installed && !live(x)).map(x => ({ id: x.id, name: x.name, app_path: x.app_path })),
    ...ST.connectors.filter(c => c.installed && !ST.sources.some(x => x.id === c.id))];
  const one = apps.length === 1 ? apps[0] : null;
  const a = el('connect');
  a.href = one ? one.app_path : '#';
  a.onclick = e => { if (one) openApp(e, one.id, one.app_path); else { e.preventDefault(); openSources(); } };
  const box = el('setups');
  box.replaceChildren(...(apps.length > 1 ? apps.map(c => appLink('Set up ' + c.name, c.id, c.app_path, 'wz-button secondary')) : []));
  if (!apps.length && !ST.sources.some(live)) box.append(h('p', 'hl-note', 'Get ' + join(ST.connectors.map(c => c.name)).replace(' and ', ' or ') + ' from the App Store.'));
  box.hidden = !box.childElementCount;
  el('wait').hidden = !ST.sources.some(live);
}
function renderFocus() {
  const iso = shown(); if (!iso) return;
  el('date').textContent = dayName(iso);
  el('when').hidden = !sel;
  for (const m of METRICS) focusPanel(P[m], iso);
  for (const b of el('metric').children) b.setAttribute('aria-checked', String(b.dataset.m === METRIC));
  for (const m of METRICS) P[m].root.classList.toggle('is-on', m === METRIC);
}
function focusPanel(X, iso) {
  const q = X.q, m = X.m, d = rec4(iso) || {};
  const F = focusOf(m, d, !!sel), T = title(m);
  q('tname').textContent = T.name;
  q('tgloss').textContent = T.gloss;
  q('tsub').textContent = T.sub; q('tsub').hidden = !T.sub;
  // The card wears the shown day's square class: same variable, same colour, never drifting.
  const card = q('hl-focus');
  card.className = 'hl-focus ' + (rec4(iso) ? SCALE[m].cls(d) : '');
  q('fnum').textContent = F.value;
  q('fof').textContent = F.of || '';
  q('fchip').textContent = F.chip || ''; q('fchip').hidden = !F.chip;
  q('fsay').textContent = F.say + (F.also ? ' ' + F.also : '');
  fillBar(q('fgoal'), F.guide);
  fillStages(q('fstages'), m === 'sleep' ? d.sleep || {} : {});
  fillRows(q('fkv'), rec4(iso) ? detailsOf(m, d, iso) : []);
  const from = rec4(iso) ? headFrom(m, d) : '';
  q('ffrom').textContent = from; q('ffrom').hidden = !from;
  card.classList.toggle('is-open', OPEN);
  q('fexp').setAttribute('aria-expanded', String(OPEN));
  q('fexp').hidden = !rec4(iso);
  card.dataset.on = inkFor(card);
  card.setAttribute('aria-label', `${T.name}, ${dayName(iso)}: ${F.value}${F.of ? ' ' + F.of : ''}${F.chip ? ', ' + F.chip : ''}. ${F.say}${F.also ? ' ' + F.also : ''}`);
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
  const scrolls = sizeGraph(X);
  X.graph.setAttribute('aria-label', `${S.name}, every day: ${tracked} ${tracked === 1 ? 'day' : 'days'} tracked${scrolls ? ', older weeks to the left' : ''}.`);
  if (rebuilt || changed || X.cell !== X.lastCell) scrollToDay(X, on, true);
  X.lastCell = X.cell;
  fitMonths(X);
  showTip(X);
}
// One legend for all three graphs (it sits under them): what the colours mean, in plain words.
function renderLegend() {
  const box = el('legend');
  if (box.childElementCount) return;
  for (const [cls, colour, word] of LEGEND) {
    const k = h('span', 'hl-key'); k.append(h('i', 'hl-cell ' + cls), (colour ? colour + ' ' : '') + word);
    box.append(k);
  }
  box.setAttribute('aria-label', 'Colours: green good, yellow okay, red needs attention, grey no data');
}
// A live skin or light/dark change repaints the fills: re-pick each card's text colour once the new sheets apply.
let inkTimer = null;
function reink() { clearTimeout(inkTimer); const go = () => { if (hasData() && newest()) for (const m of METRICS) P[m].q('hl-focus').dataset.on = inkFor(P[m].q('hl-focus')); };
  requestAnimationFrame(go); inkTimer = setTimeout(go, 400); setTimeout(go, 1500); }
new MutationObserver(reink).observe(document.documentElement, { attributes: true });
new MutationObserver(reink).observe(document.head, { childList: true });
document.addEventListener('load', e => { if (e.target.tagName === 'LINK') reink(); }, true);
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
function setOpen(v) { OPEN = !!v; for (const m of METRICS) { const c = P[m].q('hl-focus'); c.classList.toggle('is-open', OPEN); P[m].q('fexp').setAttribute('aria-expanded', String(OPEN)); c.dataset.on = inkFor(c); } }
// Switching metric (narrow windows) keeps the selected day.
function setMetric(m) {
  if (!SCALE[m] || m === METRIC) return;
  METRIC = m;
  renderFocus(); renderGraph();
  if (!wide()) el('main').scrollTop = 0;
}

// ---------- the sources sheet (the one dialog, WzSheet closes it five ways) ----------
function openSheet(title, nodes, kind = '') {
  const d = el('sheet');
  d.className = 'wz-sheet hl-sheet' + (kind ? ' hl-sheet-' + kind : '');
  const body = el('sheet-body');
  const h2 = h('h2', '', title); h2.id = 'sheet-h';
  body.replaceChildren(h2, ...nodes);
  if (window.WzSheet) WzSheet.open(d); else d.showModal();
}
// Every connector Health found, with one word for its state; one not set up yet is a 'Set up' link to its own app
// (Health never takes keys). With two or more connected, the owner picks the main source (saved server-side).
const STATE = { connected: ['Connected', 'ok'], stale: ['Behind', 'stale'], not_set_up: ['Not set up', 'off'], sign_in: ['Sign in again', 'stale'],
  needs_update: ['Needs an update', 'stale'], error: ['Not working', 'stale'], not_installed: ['Not installed', 'off'] };
const join = xs => xs.length < 2 ? xs.join('') : xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1];
function openSources() {
  const nodes = [];
  const list = h('ul', 'hl-srcs');
  const choose = ST.sources.filter(x => x.connected).length >= 2;
  for (const x of ST.sources) {
    const st = stateOf(x), [word, dot] = STATE[st] || ['Unknown', 'off'];
    const li = h('li', 'hl-src');
    li.dataset.state = st;
    li.append(h('i', 'hl-sdot ' + dot));
    const m = h('span', 'hl-src-main'), name = h('b', '', x.name + (x.account ? ' \u00b7 ' + x.account : ''));
    if (choose && x.primary) name.append(h('span', 'hl-main', 'Main'));
    m.append(name);
    const line = st === 'connected' ? 'Synced ' + ago(x.last_sync) : st === 'stale' ? 'Behind \u00b7 synced ' + ago(x.last_sync)
      : st === 'not_installed' && x.days ? 'Not installed \u00b7 ' + x.days + ' days kept' : word;
    m.append(h('small', st === 'stale' || st === 'sign_in' || st === 'needs_update' || st === 'error' ? 'hl-warn' : '', line));
    if (st === 'needs_update') m.append(h('small', 'hl-warn', 'Update ' + x.name + ' or Health'));
    else if ((x.error || x.pull_error) && st !== 'not_set_up') m.append(h('small', 'hl-warn', x.error || x.pull_error));
    li.append(m);
    const a = h('span', 'hl-src-act');
    if (choose && x.connected && !x.primary) { const b = h('button', 'wz-button secondary hl-sm', 'Make main'); b.type = 'button'; b.onclick = () => setPrimary(x.id); a.append(b); }
    if (st === 'not_set_up') a.append(appLink('Set up', x.id, x.app_path, 'wz-button hl-sm'));
    else if (st === 'sign_in') a.append(appLink('Sign in', x.id, x.app_path, 'wz-button hl-sm'));
    else if (x.installed && !(choose && x.connected && !x.primary)) a.append(appLink('Open', x.id, x.app_path, 'wz-button secondary hl-sm'));
    else if (!x.installed) { const b = h('button', 'wz-button secondary hl-sm', 'Remove'); b.type = 'button'; b.onclick = () => forget(x.id); a.append(b); }
    li.append(a);
    list.append(li);
  }
  // Known connectors that are installed but not reachable as a source yet, and the ones not installed at all.
  for (const c of ST.connectors.filter(c => c.installed && !ST.sources.some(x => x.id === c.id))) {
    const li = h('li', 'hl-src'); li.dataset.state = 'not_set_up';
    li.append(h('i', 'hl-sdot off'));
    const m = h('span', 'hl-src-main'); m.append(h('b', '', c.name), h('small', '', 'Not set up')); li.append(m);
    const a = h('span', 'hl-src-act'); a.append(appLink('Set up', c.id, c.app_path, 'wz-button hl-sm')); li.append(a);
    list.append(li);
  }
  nodes.push(list);
  if (choose) nodes.push(h('p', 'hl-note', 'The main source fills the cards; the others fill its gaps.'));
  const more = ST.connectors.filter(c => !c.installed && !ST.sources.some(x => x.id === c.id)).map(c => c.name);
  if (more.length) nodes.push(h('p', 'hl-note', 'Also works with ' + join(more) + ', from the App Store.'));
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
  // Narrow windows: one tap on the card unfolds its details in place (no sheet); wide windows always show them.
  // Wide windows: Activity always shows its details; Recovery and Sleep (the supporting data below it) fold the same way.
  X.q('hl-focus').addEventListener('click', () => { if (rec4(shown()) && (!wide() || m !== 'activity')) setOpen(!OPEN); });
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
const CACHE = 'wizard-health.screen.v6';
function saveCache() { try { localStorage.setItem(CACHE, JSON.stringify({ ST, SUM, DAYS })); } catch (e) {} }
function loadCache() {
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

// Domain Back: an open sheet closes first (WzSheet), then an unfolded card folds (narrow windows), then a past day
// returns to today, which is the root (the app stays).
const folds = () => OPEN;
function canBack() { return hasData() && (folds() || !!sel); }
function goBack() {
  if (!canBack()) return false;
  if (folds()) { setOpen(false); return true; }
  toToday(); return true;
}
(function start() {
  if (!window.WizardApp || WizardApp.version !== 1) {
    say('Wizard system functions are unavailable. Open Health through Wizard.', 'error');
  } else {
    WizardApp.ready({ name: 'wizard-health', version: '0.13.0' });
    WizardApp.back.register({ canGoBack: canBack, back: goBack });
  }
  if (loadCache()) renderAll();
  refresh().then(poll);
})();
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
