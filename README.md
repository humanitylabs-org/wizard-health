# Health

A Wizard OS health tracker over the wearables you connect. Recovery, Sleep and Activity (0-21, WHOOP's Strain) each get a title that says what it is and one summary card for the selected day, filled with the same colour as that day's square in the graph (the card wears the square's class, and its text is the kit's ink or paper colour, whichever contrasts more with the fill): a big number, a word (Ready/So-so/Run down; Enough/A bit short/Short against need; Light/Moderate/High/All out), one short sentence (plus a short one when sleep and recovery disagree) and the 2-4 supporting numbers baked in (on a phone one tap on the card unfolds them). Below, every day as a square like GitHub's contribution graph (weeks as columns, newest on the right, only tracked weeks; thumb-sized on a phone and scrolling sideways for older weeks). A wide window (680 px of app width or more) shows all three side by side, in sync; a narrow one shows one at a time with a Recovery | Sleep | Activity bar at the bottom for the thumb. Tap a square to see that day in every card and graph; the date and a Back to today pill show only then. Back folds an unfolded card, then returns to today. Sources open from the corner dot. The launcher icon is `web/icon.svg` (a silhouette the shell tints); `icon.png`/`icon.svg` are the full-colour store icon.

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
