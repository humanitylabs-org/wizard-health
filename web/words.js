'use strict';
// Health's plain words: the bands, chip labels and sentences generated from the numbers. Pure (no page access), so the
// unit tests run this very file in node. The screen reads it as window.HealthWords.
(function (root) {
  const num = v => typeof v === 'number' && isFinite(v);

  // Recovery, WHOOP's bands: 0-33 red, 34-66 yellow, 67-100 green. The same three words everywhere (card, legend).
  const recBand = v => !num(v) ? null : v >= 67 ? 'hi' : v >= 34 ? 'md' : 'lo';
  const REC_WORD = { hi: 'Ready', md: 'So-so', lo: 'Run down' };
  // Sleep against need: Enough at 85% or more, A bit short at 70-85%, Short below 70%.
  const sleepBand = (h, need) => !num(h) || !num(need) || need <= 0 ? null : h / need >= 0.85 ? 'hi' : h / need >= 0.70 ? 'md' : 'lo';
  const SLEEP_WORD = { hi: 'Enough', md: 'A bit short', lo: 'Short' };
  // Activity, WHOOP's strain bands: under 10, 10-14, 14-18, 18 and over.
  const actWord = v => !num(v) ? null : v >= 18 ? 'All out' : v >= 14 ? 'High' : v >= 10 ? 'Moderate' : 'Light';

  // One short summary per card.
  const REC_SAY = {
    now: { hi: 'Well recovered. A good day to push.', md: 'Partly recovered. A normal day is fine.', lo: 'Not recovered. Take it easy today.' },
    past: { hi: 'Well recovered. A good day to push.', md: 'Partly recovered. A normal day was fine.', lo: 'Not recovered. A day to take it easy.' },
  };
  const ACT_SAY = { 'Light': 'A light day, no hard push.', 'Moderate': 'A moderate day with some real effort.',
    'High': 'A hard day for your heart.', 'All out': 'An all-out day. Plan to recover.' };

  // Each metric's title says what it is, so no separate question is needed. Only Recovery adds one line: how it
  // differs from sleep (beginners find the two confusing).
  const TITLE = {
    recovery: { name: 'Recovery', gloss: 'how ready your body is', sub: 'Read from your heart overnight, not just your sleep.' },
    sleep: { name: 'Sleep', gloss: 'time asleep vs what you needed' },
    activity: { name: 'Activity', gloss: 'how hard your body worked' },
  };
  const title = metric => { const t = TITLE[metric]; return t ? { name: t.name, gloss: t.gloss, sub: t.sub || '' } : { name: '', gloss: '', sub: '' }; };

  // Sleep's summary: how far short of need, or enough.
  function sleepSay(asleepMin, needMin) {
    const missing = needMin - asleepMin;
    if (missing <= 0) return 'All the sleep you needed.';
    const m = Math.round(missing), d = m >= 60 ? Math.floor(m / 60) + 'h ' + (m % 60) + 'm' : m + 'm';
    return d + ' less than you needed.';
  }

  // When sleep and recovery disagree, one short sentence says why: recovery is read from the heart, not hours asleep.
  // Short sleep but Ready/So-so recovery, or Enough sleep but So-so/Run down recovery. Otherwise nothing.
  function recoveryVsSleep(score, asleepH, needH, past) {
    const r = recBand(score), s = sleepBand(asleepH, needH);
    if (!r || !s) return '';
    if (s === 'lo' && r === 'hi') return 'Short sleep, but your heart handled it.';
    if (s === 'lo' && r === 'md') return 'Short sleep, but your heart held up okay.';
    if (s === 'hi' && r === 'lo') return past ? 'Enough sleep, so something else (stress, illness, alcohol) was weighing on you.'
      : 'Enough sleep, so something else (stress, illness, alcohol) is weighing on you.';
    if (s === 'hi' && r === 'md') return past ? 'Enough sleep, so something else may have held you back.' : 'Enough sleep, so something else may be holding you back.';
    return '';
  }

  // Text on a coloured card: the WCAG contrast ratio of two [r, g, b] (0-255), and which of two inks reads better.
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const betterInk = (fill, inkA, inkB) => contrast(fill, inkA) >= contrast(fill, inkB) ? 'a' : 'b';

  const W = { num, recBand, REC_WORD, sleepBand, SLEEP_WORD, actWord, REC_SAY, ACT_SAY, TITLE, title, sleepSay, recoveryVsSleep, contrast, betterInk };
  if (typeof module === 'object' && module.exports) module.exports = W; else root.HealthWords = W;
})(typeof window !== 'undefined' ? window : globalThis);
