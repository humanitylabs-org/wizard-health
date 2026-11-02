"""Health: the owner's health tracker for a Wizard box, over any number of health-data connectors.

Health never talks to WHOOP, Oura, Garmin or any other provider and never holds their keys. It reads connector apps
that fill the `health-data` spot through the shared contract `wizard.health-data/1` (docs/health-data-contract.md),
keeps its own copy of their normalized day records, and builds Today, 7/30/90-day trends and workouts from it.

Discovery mirrors Money over bank connections: candidates come from the App Manager's grants file
(`/wizard/grants.json`: provider_endpoints_all lists every provider of the spot, provider_endpoints one; written
because wizard.md says `uses: [health-data]`), the HEALTH_SOURCES env (`id=http://host:port,...`) and a built-in list
of known connectors (WHOOP, Oura, Fitbit, Polar, Withings with their ports). Every candidate is probed at
GET /api/status in parallel; a reply with the contract names itself, so a new connector needs no code change here.
Probes are cached and kept warm by a background loop; reads never wait on a connector.

Several sources: one is the main source (the owner's pick, else the first connected in KNOWN order). Each day is the
main source's record; a metric it lacks that day is filled from another source and marked in `origins`/`filled`.

Each source is pulled with its `changed_since` cursor (only days that changed). When a source's `dataset` id changes
(Disconnect wiped it), Health drops that source's copy and reads it again.

A source that reports not_configured/disconnected, or is no longer discovered (uninstalled), stops counting at once:
its days are left out of every read. Its stored days are purged only after it has stayed that way for
HEALTH_GONE_PURGE_HOURS (24 h), so a connector restart never wipes anything. When it comes back Health reads it again
from the start.

Stdlib only. DATA_DIR holds health.sqlite3 (0600, folder 0700).
"""
import datetime as dt
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-health'
VERSION = '0.14.0'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8826')
CONTRACT = 'wizard.health-data/1'
GRANTS_FILE = os.environ.get('WIZARD_GRANTS_FILE', '/wizard/grants.json')
# Connectors Health knows by name, as Money knows SimpleFIN/Monarch/Teller: a fallback for App Managers whose grants
# file lists one endpoint per spot. Anything else that fills the spot is found through the grants file
# (provider_endpoints_all lists every provider) or HEALTH_SOURCES, and names itself. The order is also the default
# choice of main source when several are connected.
KNOWN = [
    {'id': 'wizard-whoop', 'name': 'WHOOP', 'base': 'http://wizard-whoop:8824', 'app_path': '/wizard-whoop/',
     'blurb': 'Recovery, sleep, strain and workouts from your WHOOP.'},
    {'id': 'wizard-oura', 'name': 'Oura', 'base': 'http://wizard-oura:8828', 'app_path': '/wizard-oura/',
     'blurb': 'Readiness, sleep and activity from your Oura ring.'},
    {'id': 'wizard-fitbit', 'name': 'Fitbit', 'base': 'http://wizard-fitbit:8830', 'app_path': '/wizard-fitbit/',
     'blurb': 'Sleep, heart and activity from your Fitbit or Pixel Watch.'},
    {'id': 'wizard-polar', 'name': 'Polar', 'base': 'http://wizard-polar:8832', 'app_path': '/wizard-polar/',
     'blurb': 'Recharge, sleep and training from your Polar.'},
    {'id': 'wizard-withings', 'name': 'Withings', 'base': 'http://wizard-withings:8834', 'app_path': '/wizard-withings/',
     'blurb': 'Sleep, heart and activity from your Withings devices.'},
]
STALE_HOURS = 3   # a connected source that has not synced for this long is shown as behind
ENV_SOURCES = [s.split('=', 1) for s in os.environ.get('HEALTH_SOURCES', '').split(',') if '=' in s]
PROBE_TTL = float(os.environ.get('HEALTH_PROBE_TTL', '90'))
PULL_MINUTES = float(os.environ.get('HEALTH_PULL_MINUTES', '10'))
LOOP_SECONDS = float(os.environ.get('HEALTH_LOOP_SECONDS', '60'))
LIVE = ('connected', 'syncing', 'rate_limited')
# A source reporting one of these (or no longer discovered at all) is off: its days are not used, and after
# GONE_PURGE_HOURS in a row they are deleted. needs_login and errors are not off: the data is still the owner's.
GONE = ('not_configured', 'disconnected')
GONE_PURGE_HOURS = float(os.environ.get('HEALTH_GONE_PURGE_HOURS', '24'))
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
LOCK = threading.RLock()
PULL_LOCK = threading.Lock()
WAKE = threading.Event()


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------- storage ----------------

def db():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    path = DATA / 'health.sqlite3'
    fresh = not path.exists()
    c = sqlite3.connect(path, timeout=15)
    if fresh:
        os.chmod(path, 0o600)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT, provider TEXT, app_path TEXT, base TEXT,
        dataset TEXT, cursor TEXT, last_pull TEXT, pull_error TEXT, status TEXT);
      CREATE TABLE IF NOT EXISTS days (source TEXT NOT NULL, date TEXT NOT NULL, data TEXT NOT NULL, pulled_at TEXT,
        PRIMARY KEY (source, date));
      CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    ''')
    if 'gone_since' not in {r[1] for r in c.execute('PRAGMA table_info(sources)')}:
        try:
            with c:
                c.execute('ALTER TABLE sources ADD COLUMN gone_since TEXT')
        except sqlite3.OperationalError:      # another thread added it first
            pass
    return c


def secure_files():
    db().close()
    for p in DATA.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t=None):
    return (t or _now()).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _ago(iso):
    try:
        return (_now() - dt.datetime.fromisoformat(str(iso).replace('Z', '+00:00'))).total_seconds()
    except (TypeError, ValueError):
        return 1e12


def _setting(key, value=None, clear=False):
    with LOCK:
        c = db()
        try:
            if value is not None or clear:
                with c:
                    if clear:
                        c.execute('DELETE FROM settings WHERE key=?', (key,))
                    else:
                        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
                return value
            row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row['value'] if row else None
        finally:
            c.close()


# ---------------- connectors ----------------

def _get(base, path, params=None, timeout=10):
    url = base.rstrip('/') + path + ('?' + urlencode(params) if params else '')
    req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': f'{APP_ID}/{VERSION}'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get('error')
        except ValueError:
            msg = None
        raise Problem(502, msg or f'The health connection answered {e.code}.') from None
    except (OSError, ValueError):
        raise Problem(502, 'Could not reach that health connection app.') from None


def _rank(sid):
    """Known connectors first, in KNOWN order; anything else after them, by id."""
    i = next((n for n, k in enumerate(KNOWN) if k['id'] == sid), len(KNOWN))
    return (i, str(sid or ''))


def candidates():
    """(id or None, base) pairs to probe: grants file, env, then the known list; first one per address wins."""
    out, seen = [], set()

    def add(sid, base):
        base = str(base or '').rstrip('/')
        if not re.match(r'^https?://[A-Za-z0-9._-]+(:\d+)?$', base) or base in seen:
            return
        seen.add(base)
        out.append((sid if sid and ID_RE.match(sid) else None, base))
    try:
        g = json.loads(Path(GRANTS_FILE).read_text())
        # Newer App Managers list every provider of a spot (provider_endpoints_all: target -> app id -> URL);
        # provider_endpoints keeps one endpoint per target.
        for key, many in sorted((g.get('provider_endpoints_all') or {}).items()):
            for sid, base in sorted((many if isinstance(many, dict) else {}).items(), key=lambda x: _rank(x[0])):
                add(sid, base)
        for key, base in sorted((g.get('provider_endpoints') or {}).items()):
            add(None if str(key).startswith('slot:') else key, base)
    except (OSError, ValueError, AttributeError):
        pass
    for sid, base in ENV_SOURCES:
        add(sid.strip(), base.strip())
    for k in KNOWN:
        add(k['id'], k['base'])
    return out


def _app_path(p, sid):
    p = str(p or '')
    return p if re.fullmatch(r'/[a-z0-9][a-z0-9-]{1,40}/', p) else f'/{sid}/'


def _probe(sid, base):
    """One candidate's status; None when nothing (or no health-data connector) answers there."""
    try:
        st = _get(base, '/api/status', timeout=4)
    except Problem:
        return None
    if not isinstance(st, dict) or not str(st.get('contract', '')).startswith('wizard.health-data/'):
        return None
    src = st.get('source') if isinstance(st.get('source'), dict) else {}
    real = src.get('id') if ID_RE.match(str(src.get('id') or '')) else sid
    if not real:
        return None
    known = next((k for k in KNOWN if k['id'] == real), {})
    acct = st.get('account') if isinstance(st.get('account'), dict) else {}
    scales = st.get('scales') if isinstance(st.get('scales'), dict) else {}
    return {'id': real, 'base': base, 'name': str(src.get('name') or known.get('name') or real)[:60],
            'provider': str(src.get('provider') or '')[:40], 'app_path': _app_path(src.get('app_path'), real),
            'contract_ok': st.get('contract') == CONTRACT, 'status': str(st.get('status') or 'unknown'),
            'account': acct.get('name'), 'dataset': st.get('dataset'), 'last_sync': st.get('last_sync'),
            'next_sync': st.get('next_sync'), 'history_from': st.get('history_from'), 'latest_date': st.get('latest_date'),
            'counts': st.get('counts') or {}, 'error': st.get('error'), 'scales': scales,
            'has_readiness': _has_readiness(scales, st.get('provides'))}


def _has_readiness(scales, provides=None):
    """Whether a source gives a readiness/recovery score at all. None when its scales leave readiness out (or null),
    mark it `available: false`, or its `provides.readiness` is false; a connector that sends no scales at all is an
    older one: assume it does."""
    if isinstance(provides, dict) and provides.get('readiness') is False:
        return False
    if not scales:
        return True
    r = scales.get('readiness')
    return bool(r) and not (isinstance(r, dict) and r.get('available') is False)


_PROBE = {'at': 0.0, 'list': None}
_PROBE_LOCK = threading.Lock()


def probe_all(fresh=False):
    """Every reachable health-data source. Probed in parallel and cached: an app that isn't installed takes seconds
    to fail its name lookup, and that must never sit in front of a screen tap."""
    if not fresh and _PROBE['list'] is not None and time.time() - _PROBE['at'] < PROBE_TTL:
        return [dict(x) for x in _PROBE['list']]
    with _PROBE_LOCK:
        if not fresh and _PROBE['list'] is not None and time.time() - _PROBE['at'] < PROBE_TTL:
            return [dict(x) for x in _PROBE['list']]
        cands = candidates()
        out = [None] * len(cands)

        def one(i, sid, base):
            out[i] = _probe(sid, base)
        ts = [threading.Thread(target=one, args=(i, sid, base), daemon=True) for i, (sid, base) in enumerate(cands)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(8)
        found, ids = [], set()
        for s in out:
            if s and s['id'] not in ids:
                ids.add(s['id'])
                found.append(s)
        found.sort(key=lambda s: _rank(s['id']))
        _PROBE.update(at=time.time(), list=found)
        return [dict(x) for x in found]


def _store_source(p):
    with LOCK:
        c = db()
        try:
            with c:
                c.execute('''INSERT INTO sources (id, name, provider, app_path, base, status) VALUES (?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET name=excluded.name, provider=excluded.provider,
                               app_path=excluded.app_path, base=excluded.base, status=excluded.status''',
                          (p['id'], p['name'], p['provider'], p['app_path'], p['base'], json.dumps(p)))
            return dict(c.execute('SELECT * FROM sources WHERE id=?', (p['id'],)).fetchone())
        finally:
            c.close()


def _exec(sql, args=()):
    with LOCK:
        c = db()
        try:
            with c:
                c.execute(sql, args)
        finally:
            c.close()


def pull(p):
    """Bring one source's changed days into Health's copy. Returns how many day records were written."""
    row = _store_source(p)
    if not p['contract_ok']:
        return 0
    if p.get('dataset') and row.get('dataset') and p['dataset'] != row['dataset']:
        # the connector's copy was wiped or replaced: start over for this source
        _exec('DELETE FROM days WHERE source=?', (p['id'],))
        _exec('UPDATE sources SET cursor=NULL, dataset=NULL WHERE id=?', (p['id'],))
        row['cursor'] = None
    cursor = row.get('cursor') or ''
    n, page, new_cursor = 0, None, None
    try:
        for _ in range(1000):
            q = {'changed_since': cursor, 'limit': 500}
            if page:
                q['page'] = page
            r = _get(p['base'], '/api/days', q, timeout=30)
            if r.get('dataset') and p.get('dataset') and r['dataset'] != p['dataset']:
                raise Problem(409, 'The source changed while Health was reading it; it is read again shortly.')
            days = [d for d in (r.get('days') or []) if isinstance(d, dict) and DATE_RE.match(str(d.get('date', '')))]
            with LOCK:
                c = db()
                try:
                    with c:
                        for d in days:
                            c.execute('INSERT OR REPLACE INTO days (source, date, data, pulled_at) VALUES (?,?,?,?)',
                                      (p['id'], d['date'], json.dumps(d, ensure_ascii=False), _iso()))
                finally:
                    c.close()
            n += len(days)
            new_cursor = r.get('cursor') or new_cursor
            page = r.get('next_page')
            if not page:
                break
        _exec('UPDATE sources SET cursor=?, dataset=?, last_pull=?, pull_error=NULL WHERE id=?',
              (new_cursor or cursor, p.get('dataset') or r.get('dataset'), _iso(), p['id']))
    except Problem as e:
        _exec('UPDATE sources SET pull_error=? WHERE id=?', (e.message, p['id']))
    return n


def is_gone(p):
    """Whether a probed source (None = not discovered) is off: its days must not be shown."""
    return not p or p.get('status') in GONE


def hidden_sources(live=None):
    """Ids whose days Health holds but must not use right now: off (not set up / disconnected) or no longer found."""
    live = live if live is not None else {p['id']: p for p in probe_all()}
    with LOCK:
        c = db()
        try:
            held = {r['source'] for r in c.execute('SELECT DISTINCT source FROM days')}
        finally:
            c.close()
    return {sid for sid in held if is_gone(live.get(sid))}


def purge_source(sid, keep_row=True):
    """Delete Health's copy of one source's days and reset its cursor, so a reconnect reads it from the start."""
    _exec('DELETE FROM days WHERE source=?', (sid,))
    if keep_row:
        _exec('UPDATE sources SET cursor=NULL, dataset=NULL, last_pull=NULL WHERE id=?', (sid,))
    else:
        _exec('DELETE FROM sources WHERE id=?', (sid,))


def track_presence(found, rows=None):
    """After a fresh probe: stamp when each held source went off, clear the stamp when it is back (and make it read
    again from the start), and purge sources that have stayed off for GONE_PURGE_HOURS. Returns the ids that came
    back. Only ever touches sources that are off right now."""
    live = {p['id']: p for p in found}
    rows = rows if rows is not None else _source_rows()
    back = set()
    for sid, row in rows.items():
        p = live.get(sid)
        if not is_gone(p):
            if row.get('gone_since'):
                back.add(sid)
                _exec('UPDATE sources SET gone_since=NULL, cursor=NULL, last_pull=NULL WHERE id=?', (sid,))
            continue
        if not row.get('gone_since'):
            _exec('UPDATE sources SET gone_since=? WHERE id=?', (_iso(), sid))
        elif _ago(row['gone_since']) >= GONE_PURGE_HOURS * 3600:
            # installed but off: keep the row (it is still listed); uninstalled: forget it entirely
            purge_source(sid, keep_row=bool(p))
            if p:
                _exec('UPDATE sources SET gone_since=NULL WHERE id=?', (sid,))
            elif _setting('primary') == sid:
                _setting('primary', clear=True)
    return back


def pull_all(fresh_probe=True, force=False):
    if not PULL_LOCK.acquire(blocking=force):
        return {'pulled': {}, 'note': 'Already refreshing.'}
    try:
        got = {}
        found = probe_all(fresh=fresh_probe)
        back = track_presence(found)
        rows = _source_rows()
        for p in found:
            row = rows.get(p['id']) or {}
            prev = json.loads(row['status']) if row.get('status') else {}
            due = p['status'] in LIVE and (force or p['id'] in back or not row.get('last_pull')
                   or _ago(row.get('last_pull')) > PULL_MINUTES * 60
                   or prev.get('last_sync') != p.get('last_sync') or row.get('dataset') != p.get('dataset'))
            got[p['id']] = pull(p) if due else 0
            if not due:
                _store_source(p)
        return {'pulled': got}
    finally:
        PULL_LOCK.release()


def _loop():
    time.sleep(2)
    while True:
        try:
            pull_all(fresh_probe=True)
        except Exception:  # noqa: BLE001 - one bad connector reply must not stop the loop
            pass
        WAKE.wait(LOOP_SECONDS)
        WAKE.clear()


def _source_rows():
    with LOCK:
        c = db()
        try:
            rows = {r['id']: dict(r) for r in c.execute('SELECT * FROM sources')}
            for r in c.execute('SELECT source, COUNT(*) n, MIN(date) a, MAX(date) b FROM days GROUP BY source'):
                if r['source'] in rows:
                    rows[r['source']].update(days=r['n'], first_date=r['a'], last_date=r['b'])
            return rows
        finally:
            c.close()


def primary_source(live=None, rows=None):
    """The main source the screen and tools use: the owner's pick, else the first connected source in KNOWN order
    (WHOOP, Oura, Fitbit, Polar, Withings, then any other), preferring one that already holds data."""
    rows = rows if rows is not None else _source_rows()
    live = live if live is not None else {p['id']: p for p in probe_all()}
    chosen = _setting('primary')
    # an owner's pick that is off right now is skipped (kept saved: it is the main source again once it is back)
    if chosen and (chosen in rows or chosen in live) and not is_gone(live.get(chosen)):
        return chosen
    order = sorted(set(live) | set(rows), key=_rank)
    connected = [s for s in order if s in live and live[s]['status'] in LIVE]
    for sid in connected:
        if rows.get(sid, {}).get('days'):
            return sid
    if connected:
        return connected[0]
    with_data = [sid for sid in order if rows.get(sid, {}).get('days') and not is_gone(live.get(sid))]
    if with_data:
        return with_data[0]
    return next((s for s in order if s in live), None)


def _state(status, installed, connected, last_sync):
    """The one word the Sources sheet shows: connected, stale (connected but behind), not_set_up, sign_in,
    needs_update, error or not_installed."""
    if not installed:
        return 'not_installed'
    if status == 'needs_update':
        return 'needs_update'
    if connected:
        return 'stale' if _ago(last_sync) > STALE_HOURS * 3600 else 'connected'
    return {'not_configured': 'not_set_up', 'disconnected': 'not_set_up', 'needs_login': 'sign_in'}.get(status, 'error')


def sources_info():
    """Every source Health knows (reachable now, or held from before) plus the connector apps it can use."""
    live = {p['id']: p for p in probe_all()}
    rows = _source_rows()
    primary = primary_source(live, rows)
    out = []
    for sid in sorted(set(live) | set(rows), key=_rank):
        p, r = live.get(sid), rows.get(sid, {})
        off = is_gone(p)
        held = r.get('days', 0)
        # an off source's days are not used: report none, and when the held copy will be purged
        gone_at = r.get('gone_since') if off else None
        purge_at = None
        if off and held and gone_at:
            try:
                purge_at = _iso(dt.datetime.fromisoformat(gone_at.replace('Z', '+00:00')) + dt.timedelta(hours=GONE_PURGE_HOURS))
            except ValueError:
                purge_at = None
        prev = json.loads(r['status']) if r.get('status') else {}
        base = p or prev or {}
        status = p['status'] if p else 'not_installed'
        if p and not p['contract_ok']:
            status = 'needs_update'
        connected = bool(p) and p['status'] in LIVE and p['contract_ok']
        scales = base.get('scales') or {}
        out.append({'id': sid, 'name': base.get('name') or r.get('name') or sid, 'provider': base.get('provider') or r.get('provider'),
                    'app_path': base.get('app_path') or r.get('app_path') or f'/{sid}/', 'installed': bool(p), 'status': status,
                    'state': _state(status, bool(p), connected, base.get('last_sync')),
                    'connected': connected, 'account': base.get('account'),
                    'last_sync': base.get('last_sync'), 'next_sync': base.get('next_sync') if p else None,
                    'last_pull': r.get('last_pull'), 'pull_error': r.get('pull_error'), 'error': base.get('error') if p else None,
                    'days': 0 if off else held, 'held_days': held, 'off_since': gone_at, 'purge_at': purge_at,
                    'first_date': None if off else r.get('first_date'), 'latest_date': None if off else r.get('last_date'),
                    'scales': scales, 'has_readiness': base.get('has_readiness', _has_readiness(scales)), 'primary': sid == primary})
    have = {s['id']: s for s in out}
    connectors = [{'id': k['id'], 'name': k['name'], 'app_path': k['app_path'], 'blurb': k['blurb'],
                   'installed': k['id'] in live, 'connected': bool(have.get(k['id'], {}).get('connected'))} for k in KNOWN]
    return {'sources': out, 'primary': primary, 'connectors': connectors,
            'choose_primary': sum(1 for s in out if s['connected']) >= 2}


def set_primary(body):
    """Save the owner's main source (server-side, so every device follows). `auto` (or an empty id) goes back to the
    default: the first connected source in WHOOP, Oura, Fitbit, Polar, Withings order."""
    sid = str(body.get('id') or '')
    if sid in ('', 'auto'):
        _setting('primary', clear=True)
        return status_info()
    if sid not in _source_rows() and sid not in {p['id'] for p in probe_all()}:
        raise Problem(404, 'No such health source.')
    _setting('primary', sid)
    return status_info()


def forget_source(body):
    """Remove a source that is no longer installed, with Health's copy of its days."""
    sid = str(body.get('id') or '')
    if sid in {p['id'] for p in probe_all(fresh=True)}:
        raise Problem(409, 'That source is still installed. Disconnect it in its own app first.')
    _exec('DELETE FROM days WHERE source=?', (sid,))
    _exec('DELETE FROM sources WHERE id=?', (sid,))
    if _setting('primary') == sid:
        _setting('primary', clear=True)
    return status_info()


# ---------------- reading Health's copy ----------------

def _source_arg(args):
    sid = args.get('source') or primary_source()
    if sid and not ID_RE.match(str(sid)):
        raise Problem(400, 'source must be a source id such as wizard-whoop.')
    return sid


def _view(args):
    """What a read covers: (main source, the sources in order whose days are merged). An explicit `source` reads that
    one source alone; otherwise the main source, with any metric it lacks on a day filled from the other sources."""
    live = {p['id']: p for p in probe_all()}
    off = hidden_sources(live)
    if args.get('source'):
        sid = _source_arg(args)
        return sid, [] if sid in off else [sid]
    rows = _source_rows()
    sid = primary_source(live, rows)
    if not sid:
        return None, []
    others = sorted((s for s, r in rows.items() if s != sid and r.get('days') and s not in off), key=_rank)
    return sid, ([] if sid in off else [sid]) + others


def _check_day(v, name):
    if v is None or v == '':
        return None
    if not DATE_RE.match(str(v)):
        raise Problem(400, f'{name} must be YYYY-MM-DD.')
    return str(v)


def _limit(args, default=100, cap=1000):
    try:
        n = int(args.get('limit') or default)
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    return max(1, min(cap, n))


def _days(sid, frm=None, to=None, limit=None, newest_first=True):
    if not sid:
        return []
    q, p = ['SELECT data FROM days WHERE source=?'], [sid]
    if frm:
        q.append('AND date>=?'); p.append(frm)
    if to:
        q.append('AND date<=?'); p.append(to)
    q.append('ORDER BY date ' + ('DESC' if newest_first else 'ASC'))
    if limit:
        q.append('LIMIT ?'); p.append(int(limit))
    with LOCK:
        c = db()
        try:
            return [json.loads(r['data']) for r in c.execute(' '.join(q), p)]
        finally:
            c.close()


def _latest_date(sid):
    rows = _days(sid, limit=1)
    return rows[0]['date'] if rows else None


# Per-day merge across sources. Each key is one metric: a section (readiness, sleep, load) counts as present when its
# main number is there; workouts when the source sent a list (an empty list means "none that day"); the rest when the
# value is a number. A metric the main source lacks is taken whole from the first other source that has it.
_SECTION = {'readiness': 'score', 'sleep': 'asleep_h', 'load': 'value'}
MERGE_KEYS = ('readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c', 'respiratory_rate',
              'sleep', 'load', 'calories_kcal', 'energy_kj', 'avg_hr_bpm', 'max_hr_bpm', 'steps', 'workouts')


def _present(key, v):
    if key in _SECTION:
        return isinstance(v, dict) and _num(v.get(_SECTION[key]))
    if key == 'workouts':
        return isinstance(v, list)
    return _num(v)


def merge_day(date, main, recs):
    """One day from the main source's record (`recs[main]`, may be missing), every metric it lacks filled from the
    other sources in `recs` (in order). Adds `source` (the main source) and `origins` (metric -> source id, only for
    metrics present) and `filled` (the metrics taken from another source)."""
    own = recs.get(main)
    out = dict(own) if own else {'date': date, 'updated_at': None}
    origins = {k: main for k in MERGE_KEYS if own and _present(k, own.get(k))}
    # A score the main source says is still coming (readiness.state pending: scored after waking) is not missing:
    # filling it from another wearable for an hour and then switching would only confuse.
    pending = bool(own) and (own.get('readiness') or {}).get('state') == 'pending'
    extras = dict(out.get('extras') or {})
    for sid, r in recs.items():
        if sid == main or not r:
            continue
        for k in MERGE_KEYS:
            if k not in origins and _present(k, r.get(k)) and not (pending and k == 'readiness'):
                out[k] = r[k]
                origins[k] = sid
        for prov, ex in (r.get('extras') or {}).items():
            extras.setdefault(prov, ex)
    if extras:
        out['extras'] = extras
    out['source'] = main
    out['origins'] = origins
    out['filled'] = sorted(k for k, s in origins.items() if s != main)
    return out


# A day's load repeats its source's scales.load; these keys come from scales.load when the record leaves them out,
# so every record can be judged on its own scale (targets often live only in the status).
_LOAD_KEYS = ('max', 'scale_max', 'targets', 'over_margin', 'unit')


def _load_scales(ids):
    """Source id -> its scales.load (from the live probe, else what Health stored from the last one)."""
    live = {p['id']: p for p in probe_all()}
    rows = _source_rows()
    out = {}
    for sid in ids:
        p = live.get(sid) or (json.loads(rows[sid]['status']) if rows.get(sid, {}).get('status') else {})
        sc = (p.get('scales') or {}).get('load')
        out[sid] = sc if isinstance(sc, dict) else {}
    return out


def _with_scale(d, scales):
    ld = d.get('load')
    if not isinstance(ld, dict):
        return d
    sc = scales.get((d.get('origins') or {}).get('load') or d.get('source')) or {}
    extra = {k: sc[k] for k in _LOAD_KEYS if k not in ld and k in sc}
    return dict(d, load=dict(ld, **extra)) if extra else d


def _read(view, frm=None, to=None, limit=None, newest_first=True):
    """Day records for a view: the main source's day per date, with missing metrics filled from the others."""
    main, ids = view
    if not main:
        return []
    per = {sid: {d['date']: d for d in _days(sid, frm, to, limit, newest_first)} for sid in ids}
    dates = sorted(set().union(*per.values()), reverse=newest_first)
    if limit:
        dates = dates[:int(limit)]
    scales = _load_scales(ids)
    return [_with_scale(merge_day(day, main, {sid: per[sid].get(day) for sid in ids}), scales) for day in dates]


def _view_latest(view):
    got = [d for d in (_latest_date(s) for s in view[1]) if d]
    return max(got) if got else None


def _default_range(args, view, span=30):
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    if not frm and not to:
        to = _view_latest(view)
        frm = (dt.date.fromisoformat(to) - dt.timedelta(days=span - 1)).isoformat() if to else None
    return frm, to


def _meta(sid):
    s = next((x for x in sources_info()['sources'] if x['id'] == sid), None)
    return s or {'id': sid, 'name': sid, 'scales': {}, 'connected': False, 'last_sync': None, 'last_pull': None, 'status': 'unknown'}


def _labels(meta):
    sc = meta.get('scales') or {}
    return {'readiness': (sc.get('readiness') or {}).get('label') or 'Readiness',
            'sleep_score': (sc.get('sleep_score') or {}).get('label') or 'Sleep score',
            'load': (sc.get('load') or {}).get('label') or 'Load', 'load_max': (sc.get('load') or {}).get('max')}


def _hm(h):
    m = round(h * 60)
    return f'{m // 60}h {m % 60:02d}m'


# One traffic light for all three metrics, the same rules as the screen (web/words.js): green good, yellow okay,
# red needs attention; None = no data (grey).
_LIGHT = {'hi': 'green', 'md': 'yellow', 'lo': 'red'}
# WHOOP's own strain guidance per recovery band, on its 0-21 scale. Any other scale gets the same fractions of its
# scale_max (14/21 ... 18/21), unless the source sends its own targets.
_REC_TARGET = {'hi': (14, 18), 'md': (10, 14), 'lo': (0, 10)}
_BASE_MAX, _SLACK = 21, 3
_BAND_KEY = {'hi': 'high', 'md': 'medium', 'lo': 'low'}


def _clean(x):
    """14.0 -> 14, 66.66 -> 66.7: whole numbers stay whole in JSON and in words."""
    x = math.floor(float(x) * 10 + 0.5) / 10          # half up, exactly as the screen's Math.round
    return int(x) if x.is_integer() else x


def load_targets(load=None):
    """The activity target per recovery band for a day's load: {'hi': (lo, hi), 'md': ..., 'lo': ..., 'slack': n,
    'scale_max': m, 'from': 'source'|'scale'}, or None when the load has no scale to judge it by.
    No load at all (older callers) means WHOOP's 0-21 scale."""
    if load is None:
        load = {'scale_max': _BASE_MAX}
    if not isinstance(load, dict):
        return None
    smax = load.get('scale_max') if _num(load.get('scale_max')) else load.get('max')
    smax = smax if _num(smax) and smax > 0 else None
    hints = load.get('targets') if isinstance(load.get('targets'), dict) else {}
    out = {}
    for b, key in _BAND_KEY.items():
        h = hints.get(key)
        if isinstance(h, (list, tuple)) and len(h) == 2 and all(_num(x) for x in h) and h[0] <= h[1]:
            out[b] = (_clean(h[0]), _clean(h[1]))
        elif smax:
            nice = (lambda v: int(math.floor(v + 0.5))) if smax >= 50 else _clean
            out[b] = tuple(nice(x * smax / _BASE_MAX) for x in _REC_TARGET[b])
        else:
            return None
    if _num(load.get('over_margin')) and load['over_margin'] >= 0:
        slack = _clean(load['over_margin'])                 # the source's own "a bit much" margin
    elif smax:
        slack = _clean(_SLACK * smax / _BASE_MAX)
    else:
        slack = _clean(0.75 * (out['md'][1] - out['md'][0]))
    out.update(slack=slack, scale_max=_clean(smax) if smax else None, **{'from': 'source' if hints else 'scale'})
    return out


def _target_text(b, t):
    return f"under {t['lo'][1]}" if b == 'lo' else f'{t[b][0]}-{t[b][1]}'


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rec_band(score):
    return None if not _num(score) else 'hi' if score >= 67 else 'md' if score >= 34 else 'lo'


def sleep_band(asleep_h, need_h):
    """Sleep vs need: green 85%+ ('Enough'), yellow 70-85% ('A bit short'), red under 70% ('Short'); without a need,
    by hours: 7h+, 6-7h, under 6h."""
    if not _num(asleep_h):
        return None
    pct = None
    if _num(need_h) and need_h > 0:
        pct = asleep_h / need_h
        b = 'hi' if pct >= 0.85 else 'md' if pct >= 0.70 else 'lo'
    else:
        b = 'hi' if asleep_h >= 7 else 'md' if asleep_h >= 6 else 'lo'
    return {'colour': _LIGHT[b], 'word': {'hi': 'Enough', 'md': 'A bit short', 'lo': 'Short'}[b],
            'pct_of_need': round(pct * 100) if pct is not None else None, 'by': 'need' if pct is not None else 'hours'}


def activity_match(strain, score, load=None):
    """How well a day's effort matched that day's recovery. Target from the recovery band, on the load's own scale
    (WHOOP 0-21: Ready 14-18, So-so 10-14, Run down under 10; other scales the same fractions of scale_max, or the
    source's own targets). Green inside the target (or below it on a Run down day); yellow up to the slack (3 on
    WHOOP's scale) above ('A bit much') or anywhere below ('Room to push'); red more than the slack above ('Too much
    for today'). Under-doing is never red. None when either value is missing or the load has no scale."""
    r = _rec_band(score)
    t = load_targets(load)
    if not _num(strain) or not r or not t:
        return None
    v, (lo, hi) = round(strain * 10) / 10, t[r]
    if v > hi + t['slack']:
        b, side, word = 'lo', 'over', 'Too much for today'
    elif v > hi:
        b, side, word = 'md', 'over', 'A bit much'
    elif v >= lo or r == 'lo':
        b, side, word = 'hi', 'in', 'Right amount'
    else:
        b, side, word = 'md', 'under', 'Room to push'
    return {'colour': _LIGHT[b], 'word': word, 'side': side, 'strain': v,
            'target': _target_text(r, t), 'target_low': lo, 'target_high': hi,
            'scale_max': t['scale_max'], 'target_from': t['from']}


def _lights(d):
    """The additive traffic-light fields for one day record."""
    sl, ld = d.get('sleep') or {}, d.get('load') or {}
    score = (d.get('readiness') or {}).get('score')
    rb = _rec_band(score)
    return {'recovery_colour': _LIGHT.get(rb), 'sleep_band': sleep_band(sl.get('asleep_h'), sl.get('need_h')),
            'activity_match': activity_match(ld.get('value'), score, ld)}


def _scored(d):
    r = d.get('readiness') or {}
    return r.get('score') is not None and r.get('state', 'scored') == 'scored'


def _names():
    """Source id -> its name, for every source Health knows."""
    return {s['id']: s['name'] for s in sources_info()['sources']}


def _no_score_from(d, info=None):
    """The name of the source to credit in 'No score from <source>' when a day has no readiness score because none
    of the sources it came from gives one; None otherwise (a missing score is then just 'not scored yet')."""
    if _num((d.get('readiness') or {}).get('score')):
        return None
    srcs = {s['id']: s for s in (info or sources_info())['sources']}
    used = [d.get('source')] + [v for v in (d.get('origins') or {}).values() if v != d.get('source')]
    used = [u for u in dict.fromkeys(used) if u]
    if not used or any(srcs.get(u, {}).get('has_readiness', True) for u in used):
        return None
    return srcs.get(used[0], {}).get('name') or used[0]


def _with_meta(d, names):
    """The additive per-day fields: traffic lights, and the name of every source a metric came from."""
    used = sorted(set((d.get('origins') or {}).values()) | ({d['source']} if d.get('source') else set()), key=_rank)
    return dict(d, **_lights(d), source_names={s: names.get(s, s) for s in used})


_PLAIN = {'readiness': 'recovery', 'load': 'activity', 'hrv_ms': 'HRV', 'resting_hr_bpm': 'resting HR', 'spo2_pct': 'blood oxygen',
          'skin_temp_c': 'skin temp', 'skin_temp_delta_c': 'skin temp change', 'respiratory_rate': 'breathing rate',
          'calories_kcal': 'calories', 'energy_kj': 'energy', 'avg_hr_bpm': 'average HR', 'max_hr_bpm': 'max HR'}


def daily_summary(args=None):
    """The latest day (or the given day) in one compact shape, for the screen, the agent and other apps."""
    args = args or {}
    view = _view(args)
    sid = view[0]
    day = _check_day(args.get('day'), 'day')
    info = sources_info()
    meta = next((x for x in info['sources'] if x['id'] == sid), None) or ({'id': sid, 'name': sid, 'scales': {}} if sid else {})
    names = {s['id']: s['name'] for s in info['sources']}
    base = {'source': sid, 'source_name': meta.get('name'), 'connected': bool(meta.get('connected')),
            'last_sync': meta.get('last_sync'), 'last_pull': meta.get('last_pull'), 'labels': _labels(meta)}
    if not sid:
        return dict(base, available=False, day=day, text='No health source is connected yet. Connect one (for example WHOOP) in its own app.')
    rows = _read(view, to=day, limit=4)
    if not rows or (day and rows[0]['date'] != day):
        return dict(base, available=False, day=day, text='No health data for that day yet.' if day else 'No health data synced yet.')
    d = dict(rows[0])
    d['origins'] = dict(d.get('origins') or {})
    readiness_date = d['date']
    # Readiness is scored after waking; before that the newest day has no score yet. Report the latest scored one.
    if not day and not _scored(d):
        prev = next((r for r in rows[1:] if _scored(r)), None)
        if prev:
            readiness_date = prev['date']
            keys = ['readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c']
            if not d.get('sleep'):
                keys.append('sleep')
            for k in keys:
                d[k] = prev.get(k)
                if (prev.get('origins') or {}).get(k):
                    d['origins'][k] = prev['origins'][k]
                else:
                    d['origins'].pop(k, None)
            d['filled'] = sorted(k for k, s in d['origins'].items() if s != sid)
    lb = base['labels']
    parts = []
    rd = d.get('readiness') or {}
    nofrom = _no_score_from(d, info)
    if rd.get('score') is not None:
        parts.append(f"{lb['readiness']} {rd['score']:g}%" + (f" ({rd['band']})" if rd.get('band') else '') +
                     (f", HRV {d['hrv_ms']:g} ms" if d.get('hrv_ms') is not None else '') +
                     (f", resting HR {d['resting_hr_bpm']:g} bpm" if d.get('resting_hr_bpm') is not None else ''))
    elif nofrom:
        parts.append(f"no recovery score from {nofrom}" +
                     (f", HRV {d['hrv_ms']:g} ms" if d.get('hrv_ms') is not None else '') +
                     (f", resting HR {d['resting_hr_bpm']:g} bpm" if d.get('resting_hr_bpm') is not None else ''))
    sl = d.get('sleep') or {}
    if sl.get('asleep_h') is not None:
        parts.append(f"slept {_hm(sl['asleep_h'])}" + (f" ({lb['sleep_score'].lower()} {sl['score']:g}%)" if sl.get('score') is not None else ''))
    ld = d.get('load') or {}
    if ld.get('value') is not None:
        parts.append(f"{(ld.get('label') or lb['load']).lower()} {ld['value']:g}" + (f" of {ld['max']:g}" if ld.get('max') else ''))
    if d.get('workouts'):
        parts.append(f"{len(d['workouts'])} workout" + ('s' if len(d['workouts']) != 1 else ''))
    filled = [(k, d['origins'][k]) for k in d.get('filled') or []]
    if filled:
        by = {}
        for k, s in filled:
            by.setdefault(names.get(s, s), []).append(_PLAIN.get(k, k.replace('_', ' ')))
        parts.append('; '.join(f"{', '.join(ks)} from {n}" for n, ks in by.items()))
    try:
        old = (dt.date.today() - dt.date.fromisoformat(d['date'])).days
    except ValueError:
        old = None
    m = _with_meta(d, names)
    return dict(base, available=True, day=d['date'], readiness_date=readiness_date, days_old=old,
                readiness=d.get('readiness'), hrv_ms=d.get('hrv_ms'), resting_hr_bpm=d.get('resting_hr_bpm'),
                spo2_pct=d.get('spo2_pct'), skin_temp_c=d.get('skin_temp_c'), skin_temp_delta_c=d.get('skin_temp_delta_c'),
                respiratory_rate=d.get('respiratory_rate'), sleep=d.get('sleep'), load=d.get('load'),
                calories_kcal=d.get('calories_kcal'), avg_hr_bpm=d.get('avg_hr_bpm'), max_hr_bpm=d.get('max_hr_bpm'),
                steps=d.get('steps'), workouts=d.get('workouts') or [],
                recovery_colour=m['recovery_colour'], sleep_band=m['sleep_band'], activity_match=m['activity_match'],
                origins=d['origins'], filled=d.get('filled') or [], source_names=m['source_names'], no_readiness_from=nofrom,
                text=('; '.join(parts) + '.') if parts else 'Not scored yet.')


def days_list(args):
    view = _view(args)
    frm, to = _default_range(args, view)
    names = _names()
    return {'source': view[0], 'merged_from': view[1], 'from': frm, 'to': to,
            'days': [_with_meta(d, names) for d in _read(view, frm, to, _limit(args, 100, 400))]}


def _avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 1) if xs else None


METRICS = {'readiness': lambda d: (d.get('readiness') or {}).get('score'), 'hrv': lambda d: d.get('hrv_ms'),
           'rhr': lambda d: d.get('resting_hr_bpm'), 'sleep_hours': lambda d: (d.get('sleep') or {}).get('asleep_h'),
           'sleep_score': lambda d: (d.get('sleep') or {}).get('score'), 'load': lambda d: (d.get('load') or {}).get('value'),
           'calories': lambda d: d.get('calories_kcal')}


def trends(args):
    try:
        n = int(args.get('days') or 30)
    except (TypeError, ValueError):
        raise Problem(400, 'days must be 7, 30 or 90.') from None
    if n not in (7, 30, 90):
        raise Problem(400, 'days must be 7, 30 or 90.')
    view = _view(args)
    sid = view[0]
    meta = _meta(sid) if sid else {}
    end_s = _view_latest(view)
    end = dt.date.fromisoformat(end_s) if end_s else dt.date.today()
    frm = end - dt.timedelta(days=n - 1)
    cur = _read(view, frm.isoformat(), end.isoformat(), newest_first=False)
    prev = _read(view, (end - dt.timedelta(days=2 * n - 1)).isoformat(), (frm - dt.timedelta(days=1)).isoformat())
    by = {d['date']: d for d in cur}
    series = []
    for i in range(n):
        day = (frm + dt.timedelta(days=i)).isoformat()
        d = by.get(day)
        series.append({'day': day, **{k: (f(d) if d else None) for k, f in METRICS.items()},
                       'band': ((d or {}).get('readiness') or {}).get('band')})
    summary = {}
    for k, f in METRICS.items():
        a, b = _avg([f(d) for d in cur]), _avg([f(d) for d in prev])
        summary[k] = {'avg': a, 'previous_avg': b, 'change': round(a - b, 1) if a is not None and b is not None else None}
    bands = {b: sum(1 for s in series if s['band'] == b) for b in ('high', 'medium', 'low')}
    filled = sorted({k for d in cur for k in d.get('filled') or []})
    return {'source': sid, 'merged_from': view[1], 'days': n, 'from': frm.isoformat(), 'to': end.isoformat(), 'labels': _labels(meta),
            'summary': summary, 'readiness_bands': bands, 'days_with_data': len(cur), 'series': series,
            'filled_metrics': filled}


def workouts(args):
    view = _view(args)
    frm, to = _default_range(args, view)
    out = []
    for d in _read(view, frm, to):
        for w in reversed(d.get('workouts') or []):
            out.append(dict(w, date=d['date'], source=(d.get('origins') or {}).get('workouts', d.get('source'))))
    return {'source': view[0], 'from': frm, 'to': to, 'count': len(out), 'workouts': out[:_limit(args)]}


def sleep_list(args):
    view = _view(args)
    frm, to = _default_range(args, view)
    out = [dict(d['sleep'], date=d['date'], source=(d.get('origins') or {}).get('sleep', d.get('source')))
           for d in _read(view, frm, to, _limit(args)) if d.get('sleep')]
    return {'source': view[0], 'from': frm, 'to': to, 'sleep': out}


def status_info():
    info = sources_info()
    return {'app': APP_ID, 'version': VERSION, 'primary': info['primary'], 'sources': info['sources'],
            'connectors': info['connectors'], 'connected': any(s['connected'] for s in info['sources']),
            'choose_primary': info['choose_primary']}


# ---------------- MCP ----------------

_SRC = {'source': {'type': 'string', 'description': 'source id (default: the source Health shows, e.g. wizard-whoop)'}}
_RANGE = {'type': 'object', 'properties': dict({'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive (default: 30 days up to the latest day)'},
                                               'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
                                               'limit': {'type': 'integer', 'description': 'max rows, default 100'}}, **_SRC),
          'additionalProperties': False}
RO = {'readOnlyHint': True}
TOOLS = [
    {'name': 'health_daily_summary', 'description': "The owner's latest day (or a given day) from Health: readiness/recovery score 0-100 with band (high/medium/low), HRV, resting HR, SpO2, skin temp, last night's sleep (hours, stages, score, efficiency, respiratory rate), the day's load on the source's own scale (WHOOP: strain of 21), calories and workouts, plus a one-line text. Also the screen's one traffic light (green good, yellow okay, red needs attention): recovery_colour; sleep_band (colour, word Enough/A bit short/Short, pct_of_need: green 85%+, yellow 70-85%, red under 70%); activity_match (colour, word Right amount/A bit much/Room to push/Too much for today, target strain range from that day's recovery: Ready 14-18, So-so 10-14, Run down under 10). Labels come from the source. Omit day for the latest; readiness is scored after waking, so early in the day it reports the latest scored day (readiness_date). With several wearables: `source` is the owner's main source, `origins` maps each metric to the source it came from, `filled` lists metrics taken from another source because the main one had none that day, `source_names` names them, and `no_readiness_from` names the source when it gives no recovery score at all (never invented). Activity targets scale to the source's own load scale (activity_match.scale_max).",
     'inputSchema': {'type': 'object', 'properties': dict({'day': {'type': 'string', 'description': 'YYYY-MM-DD (optional)'}}, **_SRC), 'additionalProperties': False}, 'annotations': RO},
    {'name': 'health_days', 'description': 'Day-by-day health records (readiness, HRV, resting HR, SpO2, skin temp, sleep, load, calories, workouts, extras, plus recovery_colour, sleep_band and activity_match as in health_daily_summary) between from and to, newest first. Each day is the main source record with any missing metric filled from another source: `source`, `origins` (metric -> source id), `filled`, `source_names`. Pass source=<id> to read one source alone. Use for specific dates or weeks.',
     'inputSchema': _RANGE, 'annotations': RO},
    {'name': 'health_trends', 'description': 'Averages and a day series over the last 7, 30 or 90 days for readiness, HRV, resting HR, sleep hours, sleep score, load and calories, with the change versus the previous period and the count of high/medium/low readiness days.',
     'inputSchema': {'type': 'object', 'properties': dict({'days': {'type': 'integer', 'enum': [7, 30, 90]}}, **_SRC), 'additionalProperties': False}, 'annotations': RO},
    {'name': 'health_sleep', 'description': 'Main sleeps by date range, newest first: start/end, hours asleep and in bed, light/deep/REM, score, efficiency, respiratory rate, sleep need, naps.',
     'inputSchema': _RANGE, 'annotations': RO},
    {'name': 'health_workouts', 'description': 'Workouts by date range, newest first: sport, start/end, minutes, load on the source scale, average and max HR, calories, distance, minutes per heart-rate zone 0-5.',
     'inputSchema': _RANGE, 'annotations': RO},
    {'name': 'health_status', 'description': "Which health sources Health reads (WHOOP, Oura, Fitbit, Polar, Withings or any other connector), whether each is connected (`state`: connected, stale, not_set_up, sign_in, needs_update, not_installed), which one is the main source (`primary`; the owner picks it in the Sources sheet when two or more are connected), whether it gives a recovery score (`has_readiness`), how fresh its data is (source last_sync, Health last_pull, latest day) and which connector apps exist. If nothing is connected, send the owner the connector app link it lists (e.g. [WHOOP](/wizard-whoop/)); setup happens only on that app's screen, never in chat.",
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': RO},
]


def call(name, a):
    if name == 'health_daily_summary':
        return daily_summary(a)
    if name == 'health_days':
        return days_list(a)
    if name == 'health_trends':
        return trends(a)
    if name == 'health_sleep':
        return sleep_list(a)
    if name == 'health_workouts':
        return workouts(a)
    if name == 'health_status':
        return status_info()
    raise KeyError(name)


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            try:
                v = call(p.get('name'), p.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError, AttributeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
TRUSTED_NAMES = {h.split(':')[0] for h in TRUSTED_HOSTS}   # a bare trusted host name is trusted on every shell port
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _host_ok(host):
    host = (host or '').lower()
    return host in TRUSTED_HOSTS or host.split(':')[0] in TRUSTED_NAMES


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if not _host_ok(host):
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS and urlsplit(origin).netloc != host:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def send(self, code, body=None, kind='application/json'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            if path in ('/healthz', '/api/health'):
                info = sources_info()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'primary': info['primary'],
                                       'sources': [{'id': s['id'], 'status': s['status'], 'last_pull': s['last_pull']} for s in info['sources']]})
            if path in ('/api/status', '/api/sources'):
                return self.send(200, status_info())
            if path in ('/api/summary', '/api/today'):
                return self.send(200, daily_summary(query))
            if path == '/api/days':
                return self.send(200, days_list(query))
            if path == '/api/trends':
                return self.send(200, trends(query))
            if path == '/api/workouts':
                return self.send(200, workouts(query))
            if path == '/api/sleep':
                return self.send(200, sleep_list(query))
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if any(self.headers.get(h) for h in FORWARDED):
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/refresh':
                pull_all(fresh_probe=True, force=True)
                return self.send(200, status_info())
            if path == '/api/primary':
                return self.send(200, set_primary(b))
            if path == '/api/forget':
                return self.send(200, forget_source(b))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


def main():
    secure_files()
    threading.Thread(target=_loop, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()


if __name__ == '__main__':
    main()
