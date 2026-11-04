'use strict';
// Health's plain words: the bands, chip labels and sentences generated from the numbers. Pure (no page access), so the
// unit tests run this very file in node. The screen reads it as window.HealthWords.
(function (root) {
  const num = v => typeof v === 'number' && isFinite(v);

  // Recovery, WHOOP's bands: 0-33 red, 34-66 yellow, 67-100 green. The same three words everywhere (card, legend).
  const recBand = v => !num(v) ? null : v >= 67 ? 'hi' : v >= 34 ? 'md' : 'lo';
  const REC_WORD = { hi: 'Ready', md: 'So-so', lo: 'Run down' };
  // One traffic light for all three metrics: 'hi' green (good), 'md' yellow (okay), 'lo' red (needs attention), null grey.
  // Sleep against need: Enough at 85% or more, A bit short at 70-85%, Short below 70%. Without a need, by hours:
  // 7h or more, 6-7h, under 6h.
  const sleepBand = (h, need) => !num(h) ? null
    : num(need) && need > 0 ? (h / need >= 0.85 ? 'hi' : h / need >= 0.70 ? 'md' : 'lo')
    : h >= 7 ? 'hi' : h >= 6 ? 'md' : 'lo';
  const SLEEP_WORD = { hi: 'Enough', md: 'A bit short', lo: 'Short' };
  // Activity, WHOOP's strain bands (under 10, 10-14, 14-18, 18 and over): the tooltip's plain word for the effort.
  const actWord = v => !num(v) ? null : v >= 18 ? 'All out' : v >= 14 ? 'High' : v >= 10 ? 'Moderate' : 'Light';
  // Activity is judged against that day's recovery (WHOOP's own guidance on its 0-21 scale): the target effort for each
  // recovery band. Another source's scale gets the same fractions of its scale_max (14/21 ... 18/21), unless the source
  // sends its own targets (load.targets: { high: [a, b], medium: [...], low: [0, c] }). Same rules as app/server.py.
  const TARGET = { hi: [14, 18], md: [10, 14], lo: [0, 10] };
  const BASE_MAX = 21, SLACK = 3;   // this far over the target (on WHOOP's scale) is "a little": yellow
  const BAND_KEY = { hi: 'high', md: 'medium', lo: 'low' };
  const clean = x => { const v = Math.round(x * 10) / 10; return Object.is(v, -0) ? 0 : v; };
  // The targets for one day's load: { hi, md, lo, slack, max }, or null when the load has no scale to judge it by.
  // No load at all (older callers) means WHOOP's 0-21 scale.
  function targets(load) {
    if (load === undefined || load === null) load = { scale_max: BASE_MAX };
    if (typeof load !== 'object') return null;
    let max = num(load.scale_max) ? load.scale_max : load.max;
    max = num(max) && max > 0 ? max : null;
    const hints = load.targets && typeof load.targets === 'object' ? load.targets : null;
    const out = {};
    for (const b of ['hi', 'md', 'lo']) {
      const h = hints && hints[BAND_KEY[b]];
      if (Array.isArray(h) && h.length === 2 && num(h[0]) && num(h[1]) && h[0] <= h[1]) out[b] = [clean(h[0]), clean(h[1])];
      else if (max) out[b] = TARGET[b].map(x => max >= 50 ? Math.round(x * max / BASE_MAX) : clean(x * max / BASE_MAX));
      else return null;
    }
    out.slack = num(load.over_margin) && load.over_margin >= 0 ? clean(load.over_margin)   // the source's own margin
      : max ? clean(SLACK * max / BASE_MAX) : clean(0.75 * (out.md[1] - out.md[0]));
    out.max = max;
    return out;
  }
  const targetText = (b, load) => { const t = targets(load); return !t || !t[b] ? '' : b === 'lo' ? 'under ' + t.lo[1] : t[b][0] + '\u2013' + t[b][1]; };
  // How well a day's effort matched its recovery: { band, side, word } or null when either is missing (or no scale).
  //  green: inside the target, or below it on a Run down day (rest is right);
  //  yellow: below the target ("Room to push", never red), or up to the slack above ("A bit much");
  //  red: more than the slack above ("Too much for today").
  // The value is rounded to one decimal first, as the screen shows it, so the colour matches the number.
  function actMatch(strain, score, load) {
    const r = recBand(score), t = targets(load);
    if (!num(strain) || !r || !t) return null;
    const v = Math.round(strain * 10) / 10, [lo, hi] = t[r];
    if (v > hi + t.slack) return { band: 'lo', side: 'over', word: 'Too much for today' };
    if (v > hi) return { band: 'md', side: 'over', word: 'A bit much' };
    if (v >= lo || r === 'lo') return { band: 'hi', side: 'in', word: 'Right amount' };
    return { band: 'md', side: 'under', word: 'Room to push' };
  }
  const MATCH_SAY = {
    now: { in: 'Right amount for how recovered you are.', rest: 'Taking it easy is right today.', under: 'You have room to push harder.',
      over: 'A little more than your body is ready for.', much: 'More than your body is ready for.' },
    past: { in: 'Right amount for how recovered you were.', rest: 'Taking it easy was right that day.', under: 'You had room to push harder.',
      over: 'A little more than your body was ready for.', much: 'More than your body was ready for.' },
  };
  function matchSay(m, score, past) {
    const S = MATCH_SAY[past ? 'past' : 'now'];
    if (!m) return '';
    if (m.side === 'in') return recBand(score) === 'lo' && num(score) ? S.rest : S.in;
    if (m.side === 'under') return S.under;
    return m.band === 'lo' ? S.much : S.over;
  }

  // Activity's progress bar and its one direction sentence: where the day's effort sits against its target zone, so a
  // yellow card says which way to go ("11.5 to go" vs "1.5 over"). Null when actMatch has no verdict.
  // { lo, hi, max, v, side, text }: lo/hi = the green zone, max = the bar's end (the scale's top, never below the value).
  function actGuide(strain, score, load, past) {
    const m = actMatch(strain, score, load), t = targets(load), r = recBand(score);
    if (!m || !t) return null;
    const v = Math.round(strain * 10) / 10, [lo, hi] = t[r];
    const whole = num(t.max) && t.max >= 50;
    const f = x => { const y = whole ? Math.round(x) : Math.round(x * 10) / 10; return Number.isInteger(y) ? String(y) : y.toFixed(1); };
    const max = Math.max(num(t.max) ? t.max : hi + 2 * t.slack, v);
    let text;
    if (m.side === 'under') text = past ? f(lo - v) + ' short of your target.' : f(lo - v) + ' to go to reach your target.';
    else if (m.side === 'over') text = past ? f(v - hi) + ' over your target.' : m.band === 'lo' ? f(v - hi) + ' over your target. Stop for today.' : f(v - hi) + ' over your target. Take it easy.';
    else if (r === 'lo') text = past ? 'Rest was right that day.' : 'Rest is right today. Stay under ' + f(hi) + '.';
    else if (past) text = 'Inside your target.';
    else text = v >= hi ? 'Top of your target. Stop here.' : 'In your target. Fine to stop, or go up to ' + f(hi) + '.';
    return { lo: r === 'lo' ? 0 : lo, hi, max, v, side: m.side, text };
  }

  // One short summary per card.
  const REC_SAY = {
    now: { hi: 'Well recovered. A good day to push.', md: 'Partly recovered. A normal day is fine.', lo: 'Not recovered. Take it easy today.' },
    past: { hi: 'Well recovered. A good day to push.', md: 'Partly recovered. A normal day was fine.', lo: 'Not recovered. A day to take it easy.' },
  };

  // Each metric's title says what it is, so no separate question is needed. Only Recovery adds one line: how it
  // differs from sleep (beginners find the two confusing).
  const TITLE = {
    recovery: { name: 'Recovery', gloss: 'how ready your body is', sub: 'Measured from your heart overnight, so it can differ from your sleep.' },
    sleep: { name: 'Sleep', gloss: 'did you get enough' },
    activity: { name: 'Activity', gloss: 'did it match your recovery' },
  };
  const title = metric => { const t = TITLE[metric]; return t ? { name: t.name, gloss: t.gloss, sub: t.sub || '' } : { name: '', gloss: '', sub: '' }; };

  // Sleep's summary: how far short of need, or enough. With the need's parts (usual night + catch-up for earlier short
  // nights), it says which part was missed, so a beginner sees why 6 hours can still be red.
  const mins = m => { m = Math.round(m); return m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm'; };
  const CATCH = 15;   // minutes of catch-up worth mentioning
  function sleepSay(asleepMin, needMin, usualMin) {
    const missing = needMin - asleepMin;
    if (missing <= 0) return 'All the sleep you needed.';
    const catchUp = num(usualMin) ? needMin - usualMin : 0;
    if (catchUp < CATCH) return mins(missing) + ' less than you needed.';
    if (asleepMin >= usualMin) return 'A normal night, but you are still ' + mins(missing) + ' behind from earlier short nights.';
    return mins(missing) + ' short: ' + mins(usualMin - asleepMin) + ' under a normal night, plus ' + mins(catchUp) + ' to catch up from earlier nights.';
  }
  // The need's one-line gloss for the details row: "usual 7h 36m + 2h 7m catch-up" (or just "your usual night").
  function needGloss(needMin, usualMin) {
    if (!num(usualMin) || !num(needMin)) return '';
    const catchUp = needMin - usualMin;
    return catchUp < CATCH ? 'your usual night' : 'usual ' + mins(usualMin) + ' + ' + mins(catchUp) + ' catch-up';
  }

  // When sleep and recovery disagree, one short sentence says why: recovery is read from the heart, not hours asleep.
  // Short sleep but Ready/So-so recovery, or Enough sleep but So-so/Run down recovery. Otherwise nothing.
  function recoveryVsSleep(score, asleepH, needH, past) {
    const r = recBand(score), s = num(needH) ? sleepBand(asleepH, needH) : null;   // only against a real need
    if (!r || !s) return '';
    if (s === 'lo' && r === 'hi') return 'Short sleep, but your heart still looks rested.';
    if (s === 'lo' && r === 'md') return past ? 'Your heart coped, but short sleep held this back.' : 'Your heart coped, but short sleep is holding this back.';
    if (s === 'hi' && r === 'lo') return past ? 'Enough sleep, so something else (stress, illness, alcohol) was weighing on you.'
      : 'Enough sleep, so something else (stress, illness, alcohol) is weighing on you.';
    if (s === 'hi' && r === 'md') return past ? 'Enough sleep, so something else may have held you back.' : 'Enough sleep, so something else may be holding you back.';
    return '';
  }

  // Text on a coloured card: the WCAG contrast ratio of two [r, g, b] (0-255), and which of two inks reads better.
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const betterInk = (fill, inkA, inkB) => contrast(fill, inkA) >= contrast(fill, inkB) ? 'a' : 'b';

  // Reply: one day as plain text for an unsent Chats draft (WizardApp.nav.openContextDraft), so the owner can start a
  // thread with his agent about it. The same words as the cards, plus the times and workouts the agent can act on.
  // d = the day record the cards show; o = { date, label, past, source, readinessDate, usual: { hrv, rhr }, names, time }.
  // Returns { id, title, message } inside the shell's limits; nothing is ever sent by the app.
  const PLAIN = { readiness: 'recovery', load: 'activity', hrv_ms: 'heart rate variability', resting_hr_bpm: 'resting heart rate',
    spo2_pct: 'blood oxygen', skin_temp_c: 'skin temperature', calories_kcal: 'calories' };
  function dayContext(d, o) {
    d = d || {}; o = o || {};
    const L = [], hm = h => mins(h * 60), past = !!o.past;
    const one = x => Number.isInteger(x) ? String(x) : (Math.round(x * 10) / 10).toFixed(1);
    const time = typeof o.time === 'function' ? o.time : (iso => String(iso).slice(11, 16) + ' UTC');
    const r = d.readiness || {}, score = r.score, sl = d.sleep || {}, ld = d.load || {};
    L.push(`Health day: ${o.label || o.date} (${o.date}, id day-${o.date}${o.source ? ', from ' + o.source : ''}).`);
    // Activity first, as on the screen
    if (num(ld.value)) {
      const m = actMatch(ld.value, score, ld), g = actGuide(ld.value, score, ld, past), rb = recBand(score);
      const top = num(ld.max) ? 'out of ' + ld.max : String(ld.label || ld.unit || '').toLowerCase();
      const tt = rb ? targetText(rb, ld) : '';
      L.push(`Activity: ${one(ld.value)}${top ? ' ' + top : ''}` + (m ? ` (${m.word}${tt ? ', target ' + tt + ' from recovery' : ''})` : '') + '.'
        + (g ? ' ' + g.text : m ? ' ' + matchSay(m, score, past) : ''));
    } else L.push('Activity: none recorded.');
    const move = [num(d.calories_kcal) && Math.round(d.calories_kcal) + ' kcal burned', num(d.steps) && Math.round(d.steps) + ' steps'].filter(Boolean);
    if (move.length) L.push('Moving: ' + move.join(', ') + '.');
    const ws = Array.isArray(d.workouts) ? d.workouts : [];
    if (ws.length) {
      L.push(`Workouts (${ws.length}):`);
      for (const w of ws.slice(0, 12)) {
        L.push('- ' + [w.sport || 'Workout', w.start && time(w.start) + (w.end ? '-' + time(w.end) : ''), num(w.minutes) && w.minutes + ' min',
          num((w.load || {}).value) && 'activity ' + one(w.load.value), num(w.avg_hr_bpm) && 'average heart rate ' + Math.round(w.avg_hr_bpm) + ' bpm',
          num(w.max_hr_bpm) && 'max ' + Math.round(w.max_hr_bpm) + ' bpm', num(w.calories_kcal) && Math.round(w.calories_kcal) + ' kcal',
          num(w.distance_m) && (w.distance_m / 1000).toFixed(1) + ' km'].filter(Boolean).join(', '));
      }
    } else if (Array.isArray(d.workouts)) L.push('Workouts: none.');
    // Recovery
    const rb = recBand(score);
    if (rb) {
      L.push(`Recovery: ${Math.round(score)}% (${REC_WORD[rb]})${o.readinessDate && o.readinessDate !== o.date ? ', scored on ' + o.readinessDate : ''}. `
        + REC_SAY[past ? 'past' : 'now'][rb] + ((s => s ? ' ' + s : '')(recoveryVsSleep(score, sl.asleep_h, sl.need_h, past))));
    } else L.push('Recovery: no score' + (o.noScoreFrom ? ' (' + o.noScoreFrom + ' does not give one)' : ' yet') + '.');
    const u = o.usual || {}, vs = (v, uu, f) => num(uu) ? ` (usual ${f(uu)})` : '';
    const heart = [num(d.hrv_ms) && 'Heart rate variability (HRV) ' + Math.round(d.hrv_ms) + ' ms' + vs(d.hrv_ms, u.hrv, x => Math.round(x)),
      num(d.resting_hr_bpm) && 'resting heart rate ' + Math.round(d.resting_hr_bpm) + ' bpm' + vs(d.resting_hr_bpm, u.rhr, x => Math.round(x)),
      num(d.spo2_pct) && 'blood oxygen ' + d.spo2_pct.toFixed(1) + '%', num(d.skin_temp_c) && 'skin temperature ' + d.skin_temp_c.toFixed(1) + ' \u00b0C',
      num(d.respiratory_rate) && 'breathing ' + one(d.respiratory_rate) + ' per minute'].filter(Boolean);
    if (heart.length) L.push('Heart overnight: ' + heart.join(', ') + '.');
    // Sleep
    if (num(sl.asleep_h)) {
      const sb = sleepBand(sl.asleep_h, sl.need_h), need = num(sl.need_h) && sl.need_h > 0;
      const pu = sl.need_parts || {}, um = num(pu.usual_h) ? Math.round(pu.usual_h * 60) : null;
      L.push(`Sleep: ${hm(sl.asleep_h)} asleep` + (need ? ` of ${hm(sl.need_h)} needed (${Math.round(100 * sl.asleep_h / sl.need_h)}%` : ' (')
        + `${need ? ', ' : ''}${SLEEP_WORD[sb]}).` + (need ? ' ' + sleepSay(Math.round(sl.asleep_h * 60), Math.round(sl.need_h * 60), um) : ''));
      const g = need ? needGloss(Math.round(sl.need_h * 60), um) : '';
      if (g) L.push('Need: ' + g + '.');
      const when = [sl.start && 'fell asleep ' + time(sl.start), sl.end && 'woke ' + time(sl.end), num(sl.in_bed_h) && hm(sl.in_bed_h) + ' in bed'].filter(Boolean);
      if (when.length) L.push('Night: ' + when.join(', ') + '.');
      const st = [['Deep', sl.deep_h], ['Dreaming (REM)', sl.rem_h], ['Light', sl.light_h], ['Awake', sl.awake_h]].filter(p => num(p[1]));
      if (st.length) L.push('Stages: ' + st.map(p => p[0] + ' ' + hm(p[1])).join(', ') + '.');
      const naps = Array.isArray(sl.naps) ? sl.naps.filter(n => num(n.asleep_h)) : [];
      if (naps.length) L.push('Naps: ' + naps.map(n => hm(n.asleep_h) + (n.start ? ' at ' + time(n.start) : '')).join(', ') + '.');
    } else L.push('Sleep: none recorded for this night.');
    // Metrics another source filled because the main source had none that day
    const names = o.names || {}, fill = Object.entries(d.origins || {}).filter(([, s]) => s && d.source && s !== d.source);
    if (fill.length) L.push('Filled from other sources: ' + fill.map(([k, s]) => (PLAIN[k] || k.replace(/_/g, ' ')) + ' from ' + (names[s] || s)).join(', ') + '.');
    L.push(`More in the Health app's tools (health_days, health_sleep, health_workouts for ${o.date}).`);
    return { id: ('day-' + o.date).replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 128), title: ('Health: ' + (o.label || o.date)).slice(0, 200), message: L.join('\n').slice(0, 32000) };
  }

  const W = { num, recBand, REC_WORD, sleepBand, SLEEP_WORD, actWord, TARGET, targets, targetText, actMatch, actGuide, MATCH_SAY, matchSay, REC_SAY, TITLE, title, sleepSay, needGloss, recoveryVsSleep, contrast, betterInk, dayContext };
  if (typeof module === 'object' && module.exports) module.exports = W; else root.HealthWords = W;
})(typeof window !== 'undefined' ? window : globalThis);
