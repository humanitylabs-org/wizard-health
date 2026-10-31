"""Health: the owner's health tracker for a Wizard box, over any number of health-data connectors.

Health never talks to WHOOP, Oura, Garmin or any other provider and never holds their keys. It reads connector apps
that fill the `health-data` spot through the shared contract `wizard.health-data/1` (docs/health-data-contract.md),
keeps its own copy of their normalized day records, and builds Today, 7/30/90-day trends and workouts from it.

Discovery mirrors Money over bank connections: candidates come from the App Manager's grants file
(`/wizard/grants.json` provider_endpoints, written because wizard.md says `uses: [health-data]`), the
HEALTH_SOURCES env (`id=http://host:port,...`) and a built-in list of known connectors. Every candidate is probed at
GET /api/status in parallel; a reply with the contract names itself, so a new connector needs no code change here.
Probes are cached and kept warm by a background loop; reads never wait on a connector.

Each source is pulled with its `changed_since` cursor (only days that changed). When a source's `dataset` id changes
(Disconnect wiped it), Health drops that source's copy and reads it again.

Stdlib only. DATA_DIR holds health.sqlite3 (0600, folder 0700).
"""
import datetime as dt
import json
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
VERSION = '0.5.0'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8826')
CONTRACT = 'wizard.health-data/1'
GRANTS_FILE = os.environ.get('WIZARD_GRANTS_FILE', '/wizard/grants.json')
# Connectors Health knows by name, as Money knows SimpleFIN/Monarch/Teller. Anything else that fills the spot is
# found through the grants file or HEALTH_SOURCES and names itself.
KNOWN = [{'id': 'wizard-whoop', 'name': 'WHOOP', 'base': 'http://wizard-whoop:8824', 'app_path': '/wizard-whoop/',
          'blurb': 'Recovery, sleep, strain and workouts from your WHOOP.'}]
ENV_SOURCES = [s.split('=', 1) for s in os.environ.get('HEALTH_SOURCES', '').split(',') if '=' in s]
PROBE_TTL = float(os.environ.get('HEALTH_PROBE_TTL', '90'))
PULL_MINUTES = float(os.environ.get('HEALTH_PULL_MINUTES', '10'))
LOOP_SECONDS = float(os.environ.get('HEALTH_LOOP_SECONDS', '60'))
LIVE = ('connected', 'syncing', 'rate_limited')
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
    return {'id': real, 'base': base, 'name': str(src.get('name') or known.get('name') or real)[:60],
            'provider': str(src.get('provider') or '')[:40], 'app_path': _app_path(src.get('app_path'), real),
            'contract_ok': st.get('contract') == CONTRACT, 'status': str(st.get('status') or 'unknown'),
            'account': acct.get('name'), 'dataset': st.get('dataset'), 'last_sync': st.get('last_sync'),
            'next_sync': st.get('next_sync'), 'history_from': st.get('history_from'), 'latest_date': st.get('latest_date'),
            'counts': st.get('counts') or {}, 'error': st.get('error'), 'scales': st.get('scales') or {}}


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


def pull_all(fresh_probe=True, force=False):
    if not PULL_LOCK.acquire(blocking=force):
        return {'pulled': {}, 'note': 'Already refreshing.'}
    try:
        got = {}
        rows = _source_rows()
        for p in probe_all(fresh=fresh_probe):
            row = rows.get(p['id']) or {}
            prev = json.loads(row['status']) if row.get('status') else {}
            due = (force or not row.get('last_pull') or _ago(row.get('last_pull')) > PULL_MINUTES * 60
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
    """The source the screen and tools use: the owner's pick, else the first connected source holding data."""
    rows = rows if rows is not None else _source_rows()
    live = live if live is not None else {p['id']: p for p in probe_all()}
    chosen = _setting('primary')
    if chosen and (chosen in rows or chosen in live):
        return chosen
    for sid, p in live.items():
        if p['status'] in LIVE and rows.get(sid, {}).get('days'):
            return sid
    with_data = [sid for sid, r in rows.items() if r.get('days')]
    if with_data:
        return with_data[0]
    return next(iter(live), None)


def sources_info():
    """Every source Health knows (reachable now, or held from before) plus the connector apps it can use."""
    live = {p['id']: p for p in probe_all()}
    rows = _source_rows()
    primary = primary_source(live, rows)
    out = []
    for sid in list(live) + [r for r in rows if r not in live]:
        p, r = live.get(sid), rows.get(sid, {})
        prev = json.loads(r['status']) if r.get('status') else {}
        base = p or prev or {}
        status = p['status'] if p else 'not_installed'
        if p and not p['contract_ok']:
            status = 'needs_update'
        out.append({'id': sid, 'name': base.get('name') or r.get('name') or sid, 'provider': base.get('provider') or r.get('provider'),
                    'app_path': base.get('app_path') or r.get('app_path') or f'/{sid}/', 'installed': bool(p), 'status': status,
                    'connected': bool(p) and p['status'] in LIVE, 'account': base.get('account'),
                    'last_sync': base.get('last_sync'), 'next_sync': base.get('next_sync') if p else None,
                    'last_pull': r.get('last_pull'), 'pull_error': r.get('pull_error'), 'error': base.get('error') if p else None,
                    'days': r.get('days', 0), 'first_date': r.get('first_date'), 'latest_date': r.get('last_date'),
                    'scales': base.get('scales') or {}, 'primary': sid == primary})
    have = {s['id']: s for s in out}
    connectors = [{'id': k['id'], 'name': k['name'], 'app_path': k['app_path'], 'blurb': k['blurb'],
                   'installed': k['id'] in live, 'connected': bool(have.get(k['id'], {}).get('connected'))} for k in KNOWN]
    return {'sources': out, 'primary': primary, 'connectors': connectors}


def set_primary(body):
    sid = str(body.get('id') or '')
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


def _default_range(args, sid, span=30):
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    if not frm and not to:
        to = _latest_date(sid)
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


def _scored(d):
    r = d.get('readiness') or {}
    return r.get('score') is not None and r.get('state', 'scored') == 'scored'


def daily_summary(args=None):
    """The latest day (or the given day) in one compact shape, for the screen, the agent and other apps."""
    args = args or {}
    sid = _source_arg(args)
    day = _check_day(args.get('day'), 'day')
    meta = _meta(sid) if sid else {}
    base = {'source': sid, 'source_name': meta.get('name'), 'connected': bool(meta.get('connected')),
            'last_sync': meta.get('last_sync'), 'last_pull': meta.get('last_pull'), 'labels': _labels(meta)}
    if not sid:
        return dict(base, available=False, day=day, text='No health source is connected yet. Connect one (for example WHOOP) in its own app.')
    rows = _days(sid, to=day, limit=4)
    if not rows or (day and rows[0]['date'] != day):
        return dict(base, available=False, day=day, text='No health data for that day yet.' if day else 'No health data synced yet.')
    d = dict(rows[0])
    readiness_date = d['date']
    # Readiness is scored after waking; before that the newest day has no score yet. Report the latest scored one.
    if not day and not _scored(d):
        prev = next((r for r in rows[1:] if _scored(r)), None)
        if prev:
            readiness_date = prev['date']
            for k in ('readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c'):
                d[k] = prev.get(k)
            if not d.get('sleep'):
                d['sleep'] = prev.get('sleep')
    lb = base['labels']
    parts = []
    rd = d.get('readiness') or {}
    if rd.get('score') is not None:
        parts.append(f"{lb['readiness']} {rd['score']:g}%" + (f" ({rd['band']})" if rd.get('band') else '') +
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
    try:
        old = (dt.date.today() - dt.date.fromisoformat(d['date'])).days
    except ValueError:
        old = None
    return dict(base, available=True, day=d['date'], readiness_date=readiness_date, days_old=old,
                readiness=d.get('readiness'), hrv_ms=d.get('hrv_ms'), resting_hr_bpm=d.get('resting_hr_bpm'),
                spo2_pct=d.get('spo2_pct'), skin_temp_c=d.get('skin_temp_c'), skin_temp_delta_c=d.get('skin_temp_delta_c'),
                respiratory_rate=d.get('respiratory_rate'), sleep=d.get('sleep'), load=d.get('load'),
                calories_kcal=d.get('calories_kcal'), avg_hr_bpm=d.get('avg_hr_bpm'), max_hr_bpm=d.get('max_hr_bpm'),
                steps=d.get('steps'), workouts=d.get('workouts') or [],
                text=('; '.join(parts) + '.') if parts else 'Not scored yet.')


def days_list(args):
    sid = _source_arg(args)
    frm, to = _default_range(args, sid)
    return {'source': sid, 'from': frm, 'to': to, 'days': _days(sid, frm, to, _limit(args, 100, 400))}


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
    sid = _source_arg(args)
    meta = _meta(sid) if sid else {}
    end_s = _latest_date(sid)
    end = dt.date.fromisoformat(end_s) if end_s else dt.date.today()
    frm = end - dt.timedelta(days=n - 1)
    cur = _days(sid, frm.isoformat(), end.isoformat(), newest_first=False)
    prev = _days(sid, (end - dt.timedelta(days=2 * n - 1)).isoformat(), (frm - dt.timedelta(days=1)).isoformat())
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
    return {'source': sid, 'days': n, 'from': frm.isoformat(), 'to': end.isoformat(), 'labels': _labels(meta),
            'summary': summary, 'readiness_bands': bands, 'days_with_data': len(cur), 'series': series}


def workouts(args):
    sid = _source_arg(args)
    frm, to = _default_range(args, sid)
    out = []
    for d in _days(sid, frm, to):
        for w in reversed(d.get('workouts') or []):
            out.append(dict(w, date=d['date']))
    return {'source': sid, 'from': frm, 'to': to, 'count': len(out), 'workouts': out[:_limit(args)]}


def sleep_list(args):
    sid = _source_arg(args)
    frm, to = _default_range(args, sid)
    out = [dict(d['sleep'], date=d['date']) for d in _days(sid, frm, to, _limit(args)) if d.get('sleep')]
    return {'source': sid, 'from': frm, 'to': to, 'sleep': out}


def status_info():
    info = sources_info()
    return {'app': APP_ID, 'version': VERSION, 'primary': info['primary'], 'sources': info['sources'],
            'connectors': info['connectors'], 'connected': any(s['connected'] for s in info['sources'])}


# ---------------- MCP ----------------

_SRC = {'source': {'type': 'string', 'description': 'source id (default: the source Health shows, e.g. wizard-whoop)'}}
_RANGE = {'type': 'object', 'properties': dict({'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive (default: 30 days up to the latest day)'},
                                               'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
                                               'limit': {'type': 'integer', 'description': 'max rows, default 100'}}, **_SRC),
          'additionalProperties': False}
RO = {'readOnlyHint': True}
TOOLS = [
    {'name': 'health_daily_summary', 'description': "The owner's latest day (or a given day) from Health: readiness/recovery score 0-100 with band (high/medium/low), HRV, resting HR, SpO2, skin temp, last night's sleep (hours, stages, score, efficiency, respiratory rate), the day's load on the source's own scale (WHOOP: strain of 21), calories and workouts, plus a one-line text. Labels come from the source. Omit day for the latest; readiness is scored after waking, so early in the day it reports the latest scored day (readiness_date).",
     'inputSchema': {'type': 'object', 'properties': dict({'day': {'type': 'string', 'description': 'YYYY-MM-DD (optional)'}}, **_SRC), 'additionalProperties': False}, 'annotations': RO},
    {'name': 'health_days', 'description': 'Day-by-day health records (readiness, HRV, resting HR, SpO2, skin temp, sleep, load, calories, workouts, extras) between from and to, newest first. Use for specific dates or weeks.',
     'inputSchema': _RANGE, 'annotations': RO},
    {'name': 'health_trends', 'description': 'Averages and a day series over the last 7, 30 or 90 days for readiness, HRV, resting HR, sleep hours, sleep score, load and calories, with the change versus the previous period and the count of high/medium/low readiness days.',
     'inputSchema': {'type': 'object', 'properties': dict({'days': {'type': 'integer', 'enum': [7, 30, 90]}}, **_SRC), 'additionalProperties': False}, 'annotations': RO},
    {'name': 'health_sleep', 'description': 'Main sleeps by date range, newest first: start/end, hours asleep and in bed, light/deep/REM, score, efficiency, respiratory rate, sleep need, naps.',
     'inputSchema': _RANGE, 'annotations': RO},
    {'name': 'health_workouts', 'description': 'Workouts by date range, newest first: sport, start/end, minutes, load on the source scale, average and max HR, calories, distance, minutes per heart-rate zone 0-5.',
     'inputSchema': _RANGE, 'annotations': RO},
    {'name': 'health_status', 'description': "Which health sources Health reads (e.g. WHOOP), whether each is connected, how fresh its data is (source last_sync, Health last_pull, latest day) and which connector apps exist. If nothing is connected, send the owner the connector app link it lists (e.g. [WHOOP](/wizard-whoop/)); setup happens only on that app's screen, never in chat.",
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
