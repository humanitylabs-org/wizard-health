'use strict';
// Health's plain words: the bands, chip labels and sentences generated from the numbers. Pure (no page access), so the
// unit tests run this very file in node. The screen reads it as window.HealthWords.
(function (root) {
  const num = v => typeof v === 'number' && isFinite(v);

  // Recovery, WHOOP's bands: 0-33 red, 34-66 yellow, 67-100 green. The same three words everywhere (chip, legend, sheet).
  const recBand = v => !num(v) ? null : v >= 67 ? 'hi' : v >= 34 ? 'md' : 'lo';
  const REC_WORD = { hi: 'Ready', md: 'So-so', lo: 'Run down' };
  // Sleep against need: Enough at 85% or more, A bit short at 70-85%, Short below 70%.
  const sleepBand = (h, need) => !num(h) || !num(need) || need <= 0 ? null : h / need >= 0.85 ? 'hi' : h / need >= 0.70 ? 'md' : 'lo';
  const SLEEP_WORD = { hi: 'Enough', md: 'A bit short', lo: 'Short' };
  // Activity, WHOOP's strain bands: under 10, 10-14, 14-18, 18 and over.
  const actWord = v => !num(v) ? null : v >= 18 ? 'All out' : v >= 14 ? 'High' : v >= 10 ? 'Moderate' : 'Light';

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

  // The one plain question each metric answers, shown under the picker (two short lines at most).
  const ASK = {
    recovery: { now: 'How ready is your body today?', past: 'How ready was your body that day?', sub: 'Measured from your heart overnight, not just your sleep.' },
    sleep: { now: 'Did you sleep enough last night?', past: 'Did you sleep enough that night?' },
    activity: { now: 'How hard did you push your body today?', past: 'How hard did you push your body that day?' },
  };
  const ask = (metric, past) => { const a = ASK[metric]; return a ? { q: past ? a.past : a.now, sub: a.sub || '' } : { q: '', sub: '' }; };

  // When sleep and recovery disagree, one sentence says why: recovery is read from the heart, not from hours asleep.
  // Short sleep but Ready/So-so recovery, or Enough sleep but So-so/Run down recovery. Otherwise nothing.
  function recoveryVsSleep(score, asleepH, needH, past) {
    const r = recBand(score), s = sleepBand(asleepH, needH);
    if (!r || !s) return '';
    if (s === 'lo' && r === 'hi') return past ? 'You slept short, but your heart showed you handled it.' : 'You slept short, but your heart shows you handled it.';
    if (s === 'lo' && r === 'md') return past ? 'You slept short, but your heart showed you held up okay.' : 'You slept short, but your heart shows you held up okay.';
    if (s === 'hi' && r === 'lo') return past ? 'You slept enough, but something else (stress, illness, alcohol, a hard workout) was weighing on you.'
      : 'You slept enough, but something else (stress, illness, alcohol, a hard workout) is weighing on you.';
    if (s === 'hi' && r === 'md') return past ? 'You slept enough, so something else (stress, illness, alcohol, a hard workout) may have held you back.'
      : 'You slept enough, so something else (stress, illness, alcohol, a hard workout) may be holding you back.';
    return '';
  }

  const W = { num, recBand, REC_WORD, sleepBand, SLEEP_WORD, actWord, REC_SAY, ACT_SAY, ASK, ask, recoveryVsSleep };
  if (typeof module === 'object' && module.exports) module.exports = W; else root.HealthWords = W;
})(typeof window !== 'undefined' ? window : globalThis);
