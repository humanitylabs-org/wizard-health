# Health

A Wizard OS health tracker over the wearables you connect. One metric at a time: pick Recovery, Sleep or Activity (0-21, WHOOP's Strain) at the top; one card shows it for the selected day as a big number, a plain question under the picker ("How ready is your body today?"), a coloured word (Ready/So-so/Run down; Enough/A bit short/Short against need; Light/Moderate/High/All out) and one plain sentence (plus one more when sleep and recovery disagree), with Why?/More opening the last 30 days and the supporting numbers. Below, every day as a square like GitHub's contribution graph (weeks as columns, newest on the right, only tracked weeks; thumb-sized on a phone and scrolling sideways for older weeks). Tap a square to see that day; Back returns to today. Sources open from the corner dot.

Health never talks to a provider and never takes keys. It reads connector apps that fill the `health-data` spot through the contract [`wizard.health-data/1`](docs/health-data-contract.md), the same split as Money over SimpleFIN/Monarch/Teller:

- [WHOOP](https://github.com/oscarhenrycollins/wizard-whoop) (`wizard-whoop`) today. A future Oura or Garmin connector only has to provide `health-data` and answer the contract; Health finds it through the App Manager's grants file, `HEALTH_SOURCES`, or its built-in list, and the connector names itself.
- When nothing is connected, the screen lists the connector apps and opens them ("Connect WHOOP" opens `/wizard-whoop/`).
- Health keeps its own copy of the days it read (pulled with each source's `changed_since` cursor; dropped and re-read when a source's `dataset` id changes), so screens and agent tools never wait on a connector.

## Interfaces

- Screen: `/wizard-health/`
- HTTP API (see `openapi.json`): `GET /api/status`, `/api/summary[?day=&source=]` (daily summary for other apps such as a morning report), `/api/days`, `/api/trends?days=7|30|90`, `/api/workouts`, `/api/sleep`; `POST /api/refresh`, `/api/primary`, `/api/forget`.
- MCP (`POST /mcp`, box-only): `health_daily_summary`, `health_days`, `health_trends`, `health_sleep`, `health_workouts`, `health_status`.

## Development

`python3 -m unittest discover -s tests` runs the tests against two fake connectors. `python3 tools/package.py` refreshes the inline Runtipi payload; `python3 tools/package.py --check` verifies it.
