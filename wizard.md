---
wizard: 0.2
id: wizard-health
name: Health
type: view
category: app
version: 0.5.0
summary: Your recovery, sleep or activity from a wearable like WHOOP, in one plain sentence, with every day as a square like a GitHub graph.
tags: [data]
license: GPL-3.0-only
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

A health tracker over the wearables you connect, one thing at a time, in plain words:

- **Pick Recovery, Sleep or Activity** at the top.
- **One card for that day** (today unless you tap another day): a big number, a coloured word (Recovery Low/Medium/High; Sleep Enough/A bit short/Short against what your body needed; Activity Light/Moderate/High/All out on a 0–21 scale) and one sentence anyone understands, such as "Your body is partly recovered. A normal day is fine; skip anything very hard." **Why?** / **More** opens the last 30 days and the numbers behind it (HRV, resting heart rate, blood oxygen and skin temperature; sleep stages; calories and workouts).
- **Every day as a square**, like a GitHub contribution graph: weeks run left to right with the newest on the right, Sunday to Saturday top to bottom, coloured by the picked metric. Tap a square to see that day (a Today pill and Back return to today). On a phone the squares are big enough to tap, about the last three months fit the screen and older weeks scroll sideways.

A dot in the corner shows how fresh your sources are and opens them.

Health reads connector apps that fill the `health-data` spot (contract `wizard.health-data/1`, see `docs/health-data-contract.md`): WHOOP today, other wearables later, with no change to Health. Connecting happens in each connector's own app; Health never asks for a wearable's keys. It keeps its own copy of the days it has read, so the screen and your agent stay fast, and other apps (such as a morning report) can read the daily summary at `GET /api/summary`.
