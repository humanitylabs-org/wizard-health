---
wizard: 0.2
id: wizard-health
name: Health
type: view
category: app
version: 0.8.0
summary: Your recovery, sleep and activity from a wearable like WHOOP, each in one plain sentence, with every day as a square like a GitHub graph.
tags: [data]
license: GPL-3.0-only
icon: icon.png
source: https://github.com/oscarhenrycollins/wizard-health
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 8826
  path: /api
  spec: openapi.json
mcp:
  path: /mcp
  port: 8826
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 8826
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [health]
system: false
uses: [health-data]
needs: []
health: /healthz
---

# Health

A health tracker over the wearables you connect, in plain words. Recovery, Sleep and Activity each get:

- **A plain question** ("How ready is your body today?").
- **One card for that day** (today unless you tap another day): a big number, a coloured word (Recovery Ready/So-so/Run down; Sleep Enough/A bit short/Short against what your body needed; Activity Light/Moderate/High/All out on a 0–21 scale) and one sentence anyone understands, such as "Your body is partly recovered. A normal day is fine; skip anything very hard." **Why?** / **More** opens the last 30 days and the numbers behind it (heart rate variability, resting heart rate, blood oxygen and skin temperature; sleep stages; calories and workouts).
- **Every day as a square**, like a GitHub contribution graph: weeks run left to right with the newest on the right, Sunday to Saturday top to bottom. Tap a square to see that day everywhere (a Today pill and Back return to today).

In a wide window all three sit side by side and move together. In a narrow window (a phone) you see one at a time and pick Recovery, Sleep or Activity in a bar at the bottom, within reach of your thumb; the squares are big enough to tap, about the last three months fit the screen and older weeks scroll sideways.

A dot in the corner shows how fresh your sources are and opens them.

Health reads connector apps that fill the `health-data` spot (contract `wizard.health-data/1`, see `docs/health-data-contract.md`): WHOOP today, other wearables later, with no change to Health. Connecting happens in each connector's own app; Health never asks for a wearable's keys. It keeps its own copy of the days it has read, so the screen and your agent stay fast, and other apps (such as a morning report) can read the daily summary at `GET /api/summary`.
