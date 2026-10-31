---
name: wizard-health
description: Use when the owner asks about his health, recovery, readiness, HRV, sleep, strain, workouts or trends.
version: 0.6.0
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [health, recovery, sleep, strain, workouts, whoop]
    related_skills: [wizard-whoop]
---
# Health

The owner's health tracker at `/wizard-health/`. It reads wearable connector apps (WHOOP today) through the `health-data` contract and keeps its own copy, so its tools answer fast. Tools: `mcp__wizard__wizard_health__<tool>`.

- "How did I sleep / what's my recovery today / am I ready to train": `health_daily_summary` (omit `day` for the latest). Readiness is scored after waking, so early in the day the summary reports the latest scored day (`readiness_date`); say which day it is.
- Specific dates or weeks: `health_days` with `from`/`to` (YYYY-MM-DD, inclusive). Sleep only: `health_sleep`. Workouts: `health_workouts` (sport, minutes, load, HR, calories, zones).
- "How am I trending": `health_trends` with `days` 7, 30 or 90 (averages, change vs the previous period, high/medium/low readiness counts).
- Use the source's own words from `labels`: WHOOP calls readiness "Recovery", sleep score "Sleep performance" and the day's load "Strain" (0-21). Bands: high 67-100, medium 34-66, low 0-33 (WHOOP green/yellow/red). HRV is RMSSD in ms; skin temperature in Celsius; sleep hours exclude awake time.
- Say how fresh the data is (`last_sync` from the source) and don't give medical diagnoses. If a value is missing, say the wearable has not scored it yet.
- Connection or setup questions ("connect my WHOOP", "it says sign in again", "add my Oura"): call `health_status`, then send the owner the connector app link it lists, e.g. `[WHOOP](/wizard-whoop/)`, and say that app's own screen walks him through it. Never walk him through a provider's developer dashboard in chat, and never ask for or accept keys, passwords, sign-in codes or callback addresses in chat. Health itself never takes keys.
- If Health shows nothing new but the wearable synced, the dot in the screen's top corner opens Sources, which has Refresh; `health_status` shows each source's `last_sync` and Health's `last_pull`.
- The screen shows one metric at a time: a Recovery | Sleep | Activity picker, one card for the selected day (big number, a coloured word and one plain sentence; Why?/More opens 30 days plus HRV, resting HR, SpO2 and skin temperature, sleep stages, or calories and workouts), and a GitHub-style graph of every day in that metric. Each metric shows its plain question under the picker (Recovery: "How ready is your body today?", measured from the heart overnight, not just sleep; Sleep: "Did you sleep enough last night?"; Activity: "How hard did you push your body today?"). When sleep was Short but recovery is So-so/Ready (or sleep was Enough but recovery is So-so/Run down), the Recovery card adds one sentence explaining the difference. Words: Recovery **Run down** <34 / **So-so** 34-66 / **Ready** 67+; Sleep **Enough** at 85%+ of need, **A bit short** 70-85%, **Short** under 70%; **Activity** (the screen's word for WHOOP Strain, 0-21) Light <10, Moderate 10-14, High 14-18, All out 18+. When talking to the owner, prefer the screen's words and plain sentences ("you slept 4h 23m less than your body needed"); say "heart rate variability (HRV), higher usually means more recovered" rather than bare HRV; tool fields still say `load`/strain.
- Other apps on the box read the same daily summary at `http://wizard-health:8826/api/summary` (scripts: `http://127.0.0.1:8826/api/summary`).
