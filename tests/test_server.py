"""Tests for Health against fake health-data connectors (contract wizard.health-data/1): multi-source discovery
from the grants file, HEALTH_SOURCES and the known list; pulling with changed_since cursors and paging; dataset
resets; source-neutral labels; a removed source; a newer contract; summary, trends, workouts; HTTP and MCP.
Run: python3 -m unittest discover -s tests"""
import base64
import datetime as dt
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
END = dt.date(2026, 10, 20)


def make_day(date, i, label='Strain', mx=21, provider='whoop'):
    return {'date': date, 'updated_at': date + 'T08:00:00.000Z',
            'readiness': {'score': [25, 50, 80][i % 3], 'band': ['low', 'medium', 'high'][i % 3], 'state': 'scored', 'calibrating': False},
            'hrv_ms': 60.0 + i % 7, 'resting_hr_bpm': 50 + i % 5, 'spo2_pct': 96.2, 'skin_temp_c': 33.7, 'skin_temp_delta_c': None,
            'respiratory_rate': 15.1,
            'sleep': {'start': date + 'T00:00:00.000Z', 'end': date + 'T07:30:00.000Z', 'asleep_h': 7.0, 'in_bed_h': 7.5, 'awake_h': 0.5,
                      'light_h': 3.5, 'deep_h': 1.5, 'rem_h': 2.0, 'score': 85, 'efficiency_pct': 93, 'respiratory_rate': 15.1, 'need_h': 8.0, 'naps': []},
            'load': {'value': 10.0 + i % 5, 'max': mx, 'label': label}, 'calories_kcal': 2200 + i, 'energy_kj': 9200 + i,
            'avg_hr_bpm': 66, 'max_hr_bpm': 160, 'steps': 8000 + i,
            'workouts': ([{'id': f'w{i}', 'sport': 'running', 'start': date + 'T10:00:00.000Z', 'end': date + 'T10:40:00.000Z', 'minutes': 40,
                           'load': {'value': 9.5, 'max': mx, 'label': label}, 'avg_hr_bpm': 150, 'max_hr_bpm': 175, 'calories_kcal': 450,
                           'energy_kj': 1880, 'distance_m': 7000, 'zones_min': [1, 2, 10, 20, 6, 1]}] if i % 2 == 0 else []),
            'extras': {provider: {'note': 'fake'}}}


class Connector:
    """A fake contract connector: status + days with paging and a changed_since cursor."""

    def __init__(self, sid, name, provider, label, mx, n, contract='wizard.health-data/1'):
        self.sid, self.name, self.provider, self.label, self.mx, self.contract = sid, name, provider, label, mx, contract
        self.dataset = sid + '-ds1'
        self.seq = 1
        self.days = {}
        for i in range(n):
            d = (END - dt.timedelta(days=n - 1 - i)).isoformat()
            self.days[d] = (make_day(d, i, label, mx, provider), 1)
        self.calls = []
        self.scales = None          # None = the default WHOOP-like scales below
        self.status = 'connected'
        self.last_sync = '2026-10-20T08:00:00.000Z'

    def change(self, date, **kv):
        self.seq += 1
        day, _ = self.days[date]
        day = dict(day, **kv)
        self.days[date] = (day, self.seq)
        self.last_sync = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    def wipe_and_reload(self):
        self.dataset = self.sid + '-ds2'
        self.seq += 1
        self.days = {d: (v, self.seq) for d, (v, _) in list(self.days.items())[-3:]}

    def handler(self):
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                me.calls.append((u.path, q))
                if u.path == '/api/status':
                    return self.reply(200, {'contract': me.contract, 'source': {'id': me.sid, 'provider': me.provider, 'name': me.name, 'app_path': f'/{me.sid}/'},
                                            'status': me.status, 'account': {'name': 'Sam'}, 'dataset': me.dataset, 'last_sync': me.last_sync,
                                            'next_sync': None, 'history_from': min(me.days), 'latest_date': max(me.days),
                                            'counts': {'days': len(me.days)}, 'error': None,
                                            'scales': me.scales if me.scales is not None else {
                                                'readiness': {'label': 'Recovery' if me.provider == 'whoop' else 'Readiness', 'min': 0, 'max': 100},
                                                'sleep_score': {'label': 'Sleep performance' if me.provider == 'whoop' else 'Sleep score', 'min': 0, 'max': 100},
                                                'load': {'label': me.label, 'min': 0, 'max': me.mx}}})
                if u.path == '/api/days':
                    since = int(q['changed_since'].split('.')[1]) if q.get('changed_since') else 0
                    page = json.loads(base64.urlsafe_b64decode(q['page'])) if q.get('page') else {}
                    hw = page.get('hw', me.seq)
                    lim = int(q.get('limit') or 100)
                    dates = sorted(d for d, (_, s) in me.days.items() if since < s <= hw and d > page.get('after', ''))
                    more = len(dates) > lim
                    dates = dates[:lim]
                    nxt = base64.urlsafe_b64encode(json.dumps({'after': dates[-1], 'hw': hw}).encode()).decode() if more else None
                    return self.reply(200, {'contract': me.contract, 'source': me.sid, 'dataset': me.dataset,
                                            'days': [me.days[d][0] for d in dates], 'next_page': nxt, 'cursor': f'x.{hw}'})
                self.reply(404, {'error': 'Not found.'})
        return H


def serve(handler):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def http(port, method, path, body=None, headers=None):
    h = {'Host': f'127.0.0.1:{port}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class HealthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.whoop = Connector('wizard-whoop', 'WHOOP', 'whoop', 'Strain', 21, 120)
        cls.oura = Connector('fake-oura', 'Oura', 'oura', 'Activity', 100, 40)
        cls.srv_w = serve(cls.whoop.handler())
        cls.srv_o = serve(cls.oura.handler())
        cls.tmp = tempfile.mkdtemp()
        grants = Path(cls.tmp) / 'grants.json'
        # what the App Manager writes for `uses: [health-data]`: one endpoint under the spot key
        grants.write_text(json.dumps({'outgoing': {'slot:health-data': 'x'}, 'incoming': [], 'enforce': False,
                                      'provider_endpoints': {'slot:health-data': f'http://127.0.0.1:{cls.srv_w.server_address[1]}'}}))
        os.environ.update({'DATA_DIR': cls.tmp + '/private', 'WIZARD_GRANTS_FILE': str(grants), 'PORT': '18998',
                           'HEALTH_SOURCES': f'fake-oura=http://127.0.0.1:{cls.srv_o.server_address[1]}',
                           'WIZARD_TRUSTED_HOSTS': 'box.example.ts.net'})
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = importlib.reload(server)
        # the built-in WHOOP address does not resolve in tests; point it at a closed loopback port instead
        cls.S.KNOWN = [dict(cls.S.KNOWN[0], base='http://127.0.0.1:9')]
        cls.app = serve(cls.S.Handler)
        cls.port = cls.app.server_address[1]
        cls.S.TRUSTED_HOSTS.add(f'127.0.0.1:{cls.port}')

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv_w, cls.srv_o, cls.app):
            s.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_1_discovery_and_pull(self):
        S = self.S
        found = S.probe_all(fresh=True)
        self.assertEqual([p['id'] for p in found], ['wizard-whoop', 'fake-oura'])   # grants first, then env; named by the source
        self.assertEqual(found[1]['name'], 'Oura')
        r = S.pull_all(force=True)
        self.assertEqual(r['pulled'], {'wizard-whoop': 120, 'fake-oura': 40})
        pages = [c for c in self.whoop.calls if c[0] == '/api/days']
        self.assertEqual(len(pages), 1)                         # 120 days fit one 500-day page
        self.assertEqual(pages[0][1]['changed_since'], '')
        info = S.sources_info()
        self.assertEqual(info['primary'], 'wizard-whoop')
        self.assertEqual({s['id']: s['days'] for s in info['sources']}, {'wizard-whoop': 120, 'fake-oura': 40})
        self.assertTrue(all(s['connected'] and s['installed'] for s in info['sources']))
        self.assertEqual(info['connectors'][0]['id'], 'wizard-whoop')

        # nothing changed upstream -> the next pull asks with the cursor and gets nothing
        self.whoop.calls.clear()
        self.assertEqual(S.pull(S.probe_all(fresh=True)[0]), 0)
        self.assertEqual(self.whoop.calls[-1][1]['changed_since'], 'x.1')

        # a late re-score upstream -> only that day comes over, and Health's copy shows it
        self.whoop.change('2026-10-18', hrv_ms=99.0)
        self.assertEqual(S.pull_all()['pulled']['wizard-whoop'], 1)   # due because the source's last_sync moved
        self.assertEqual(S.days_list({'from': '2026-10-18', 'to': '2026-10-18'})['days'][0]['hrv_ms'], 99.0)

    def test_2_reads_and_labels(self):
        S = self.S
        t = S.daily_summary()
        self.assertTrue(t['available'])
        self.assertEqual(t['day'], END.isoformat())
        self.assertEqual(t['source_name'], 'WHOOP')
        self.assertIn('Recovery', t['text'])
        self.assertIn('strain', t['text'])
        self.assertIn('of 21', t['text'])
        self.assertEqual(t['labels']['load'], 'Strain')
        # 0.9 additive traffic-light fields, the same in the summary and in every day record
        self.assertIn(t['recovery_colour'], ('green', 'yellow', 'red'))
        self.assertEqual(t['sleep_band'], {'colour': 'green', 'word': 'Enough', 'pct_of_need': 88, 'by': 'need'})   # 7h of 8h
        rows = S.days_list({})['days']
        self.assertTrue(all('activity_match' in r and 'sleep_band' in r for r in rows))
        self.assertEqual(rows[0]['activity_match'], t['activity_match'])
        tr = S.trends({'days': 30})
        self.assertEqual(len(tr['series']), 30)
        self.assertEqual(tr['days_with_data'], 30)
        self.assertEqual(sum(tr['readiness_bands'].values()), 30)
        self.assertIsNotNone(tr['summary']['hrv']['change'])
        with self.assertRaises(S.Problem):
            S.trends({'days': 12})
        w = S.workouts({})
        self.assertEqual(w['count'], 15)                           # every other day over the last 30
        self.assertEqual(w['workouts'][0]['date'], END.isoformat() if 119 % 2 == 0 else (END - dt.timedelta(days=1)).isoformat())
        self.assertEqual(S.sleep_list({'limit': 5})['sleep'][0]['asleep_h'], 7.0)
        with self.assertRaises(S.Problem):
            S.days_list({'from': 'yesterday'})
        # the other source on demand, and as the shown source: its own words, no WHOOP terms
        o = S.daily_summary({'source': 'fake-oura'})
        self.assertIn('Readiness', o['text'])
        self.assertIn('activity', o['text'])
        self.assertNotIn('strain', o['text'].lower())
        S.set_primary({'id': 'fake-oura'})
        self.assertEqual(S.daily_summary()['source'], 'fake-oura')
        self.assertEqual(S.trends({'days': 7})['labels']['load'], 'Activity')
        S.set_primary({'id': 'wizard-whoop'})
        with self.assertRaises(S.Problem):
            S.set_primary({'id': 'nope'})

    def test_3_readiness_not_scored_yet(self):
        S = self.S
        self.whoop.change(END.isoformat(), readiness={'score': None, 'band': None, 'state': 'pending', 'calibrating': False}, hrv_ms=None)
        S.pull_all()
        t = S.daily_summary()
        self.assertEqual(t['day'], END.isoformat())
        self.assertEqual(t['readiness_date'], (END - dt.timedelta(days=1)).isoformat())
        self.assertIsNotNone(t['readiness']['score'])

    def test_4_dataset_reset_removed_source_and_newer_contract(self):
        S = self.S
        self.oura.wipe_and_reload()          # the connector was disconnected and reconnected: a new copy
        S.pull_all()
        self.assertEqual(next(s for s in S.sources_info()['sources'] if s['id'] == 'fake-oura')['days'], 3)
        # source uninstalled: kept with its days, marked not installed, then removed on request
        port = self.srv_o.server_address[1]
        self.srv_o.shutdown(); self.srv_o.server_close()
        S.probe_all(fresh=True)
        o = next(s for s in S.sources_info()['sources'] if s['id'] == 'fake-oura')
        # kept for now (purged after 24 h off) but no longer used: no days count, held_days says what is kept
        self.assertEqual((o['installed'], o['status'], o['days'], o['held_days']), (False, 'not_installed', 0, 3))
        with self.assertRaises(S.Problem):
            S.forget_source({'id': 'wizard-whoop'})       # still installed
        S.forget_source({'id': 'fake-oura'})
        self.assertNotIn('fake-oura', [s['id'] for s in S.sources_info()['sources']])
        self.srv_o = type(self).srv_o = ThreadingHTTPServer(('127.0.0.1', port), self.oura.handler())
        threading.Thread(target=self.srv_o.serve_forever, daemon=True).start()
        # a connector speaking a newer contract is listed but not read
        self.oura.contract = 'wizard.health-data/2'
        S.pull_all(force=True)
        o = next(s for s in S.sources_info()['sources'] if s['id'] == 'fake-oura')
        self.assertEqual((o['status'], o['days']), ('needs_update', 0))
        self.oura.contract = 'wizard.health-data/1'

    def test_5_http_and_mcp(self):
        for path in ('/healthz', '/api/status', '/api/summary', '/api/summary?day=2026-10-10', '/api/days', '/api/trends?days=90',
                     '/api/workouts', '/api/sleep', '/wizard-health/api/status', '/', '/app.js', '/words.js', '/styles.css'):
            code, body = http(self.port, 'GET', path)
            self.assertEqual(code, 200, path)
        code, body = http(self.port, 'GET', '/api/summary?day=2026-10-10')
        self.assertEqual(json.loads(body)['day'], '2026-10-10')
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8444'})[0], 200)
        self.assertEqual(http(self.port, 'POST', '/api/refresh', {}, {'Host': 'box.example.ts.net:8444', 'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(http(self.port, 'POST', '/api/refresh', {})[0], 200)
        code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        names = [t['name'] for t in json.loads(body)['result']['tools']]
        self.assertEqual(names, ['health_daily_summary', 'health_days', 'health_trends', 'health_sleep', 'health_workouts', 'health_status'])
        for name, args in [('health_daily_summary', {}), ('health_daily_summary', {'day': '2026-10-01'}), ('health_days', {'from': '2026-10-01'}),
                           ('health_trends', {'days': 7}), ('health_sleep', {}), ('health_workouts', {}), ('health_status', {})]:
            code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            self.assertFalse(json.loads(body)['result']['isError'], (name, body[:300]))
        code, _ = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, {'X-Forwarded-For': '100.1.1.1'})
        self.assertEqual(code, 403)

    def test_7_one_view_no_looks(self):
        """0.4 is one screen: the looks endpoint and the look/motion scripts are gone; the summary stays."""
        self.assertEqual(http(self.port, 'GET', '/api/look')[0], 404)
        for path in ('/motion.js', '/looks.js'):
            self.assertEqual(http(self.port, 'GET', path)[0], 404, path)
        self.assertEqual(http(self.port, 'GET', '/api/summary')[0], 200)

    def test_8_report_section(self):
        """0.15 morning report section (wizard.report-section/1): card + Monday lines, final once the night has settled."""
        S, utc = self.S, dt.timezone.utc
        self.whoop.change(END.isoformat(), readiness={'score': 80, 'band': 'high', 'state': 'scored', 'calibrating': False})
        S.pull_all(force=True)
        q = {'date': END.isoformat(), 'tz': 'Asia/Bangkok'}
        r = S.report_section(q, now=dt.datetime(2026, 10, 20, 8, 0, tzinfo=utc))       # night ended 07:30Z, 30 min ago
        self.assertEqual((r['report'], r['app'], r['date']), (1, 'wizard-health', END.isoformat()))
        sec = r['sections'][0]
        self.assertEqual((sec['id'], sec['title'], sec['link'], sec['wait'], sec['final']), ('wizard-health', 'Health', '/wizard-health/', True, False))
        self.assertEqual(sec['card']['kind'], 'health')
        self.assertEqual(sec['card']['label'], 'Last night so far')
        self.assertEqual([t['name'] for t in sec['card']['tiles']], ['Recovery', 'Sleep'])
        self.assertEqual(sec['card']['tiles'][1]['value'], '7h 0m')
        self.assertEqual(len(sec['card']['strip']['days']), 7)
        self.assertEqual(sec['lines'], [])                                               # a Tuesday: no weekly look
        r = S.report_section(q, now=dt.datetime(2026, 10, 20, 9, 30, tzinfo=utc))       # 2 h later: final
        self.assertTrue(r['sections'][0]['final'])
        self.assertEqual(r['sections'][0]['card']['label'], 'Last night')
        self.assertTrue(S.report_section(dict(q, settle_h='0'), now=dt.datetime(2026, 10, 20, 7, 31, tzinfo=utc))['sections'][0]['final'])
        later = S.report_section({'date': '2026-10-22'}, now=dt.datetime(2026, 10, 22, 9, 0, tzinfo=utc))['sections'][0]
        self.assertEqual((later['card']['day'], later['card']['label'], later['final']), (END.isoformat(), 'Latest scored', False))
        monday = S.report_section({'date': '2026-10-19', 'tz': 'Asia/Bangkok'})['sections'][0]
        self.assertIn("**Last 7 nights' sleep**", monday['lines'])
        self.assertTrue(any(ln.startswith('- Slept 7h 0m a night') for ln in monday['lines']))
        code, body = http(self.port, 'GET', '/api/report?date=2026-10-20&tz=Asia/Bangkok')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['sections'][0]['id'], 'wizard-health')
        self.assertEqual(http(self.port, 'GET', '/wizard-health/api/report?date=2026-10-20')[0], 200)
        self.assertEqual(http(self.port, 'GET', '/api/report?date=20-10-2026')[0], 400)

    def test_6_empty_state(self):
        """No connector reachable and nothing held: the screen gets the connector apps to open."""
        S = self.S
        saved = (S.GRANTS_FILE, S.ENV_SOURCES, S.DATA)
        tmp = Path(tempfile.mkdtemp())
        try:
            S.GRANTS_FILE, S.ENV_SOURCES, S.DATA = '/nonexistent', [], tmp
            S._PROBE.update(at=0, list=None)
            st = S.status_info()
            self.assertEqual(st['sources'], [])
            self.assertIsNone(st['primary'])
            self.assertEqual([(c['id'], c['app_path'], c['installed']) for c in st['connectors']], [('wizard-whoop', '/wizard-whoop/', False)])
            self.assertFalse(S.daily_summary()['available'])
            self.assertEqual(S.report_section({'date': '2026-10-20'})['sections'], [])   # nothing connected: no section
        finally:
            S.GRANTS_FILE, S.ENV_SOURCES, S.DATA = saved
            S._PROBE.update(at=0, list=None)
            shutil.rmtree(tmp, ignore_errors=True)


class MultiSourceTest(unittest.TestCase):
    """0.10: several connectors at once. Discovery through the grants file's provider_endpoints_all (every provider)
    or, with an older App Manager, the built-in list; one main source (default in WHOOP, Oura, Fitbit, Polar, Withings
    order, the owner's pick saved server-side); per-day fill of missing metrics with their origin; a source with no
    readiness score; per-source states for the Sources sheet."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = S = importlib.reload(server)          # the full built-in list (HealthTest narrows it)
        cls.saved = (S.GRANTS_FILE, S.ENV_SOURCES, S.DATA, S.KNOWN)
        cls.tmp = Path(tempfile.mkdtemp())
        # WHOOP: 10 days, its own 0-21 scale with scale_max; Oura: 12 days (two newer), a 0-100 activity score
        cls.whoop = Connector('wizard-whoop', 'WHOOP', 'whoop', 'Strain', 21, 10)
        cls.oura = Connector('wizard-oura', 'Oura', 'oura', 'Activity', 100, 12)
        for d, (day, seq) in list(cls.whoop.days.items()):
            day['load']['scale_max'] = 21
        cls.gap = (END - dt.timedelta(days=3)).isoformat()
        day, seq = cls.whoop.days[cls.gap]
        cls.whoop.days[cls.gap] = (dict(day, sleep=None, steps=None), seq)     # WHOOP missed that night's sleep and steps
        # Withings: no readiness score at all (its scales have none), heart numbers and sleep only
        cls.withings = Connector('wizard-withings', 'Withings', 'withings', 'Steps', 0, 5)
        cls.withings.scales = {'sleep_score': {'label': 'Sleep score', 'min': 0, 'max': 100}, 'load': None}
        for d, (day, seq) in list(cls.withings.days.items()):
            cls.withings.days[d] = (dict(day, readiness=None, load=None, workouts=None, hrv_ms=41.0, resting_hr_bpm=58), seq)
        # Fitbit-shaped: readiness marked unavailable, Active Zone Minutes with no top; targets and margin only in scales
        cls.fitbit = Connector('wizard-fitbit', 'Fitbit', 'fitbit', 'Active Zone Minutes', None, 4)
        cls.fitbit.scales = {'readiness': {'label': 'Readiness', 'min': 0, 'max': 100, 'available': False},
                             'load': {'label': 'Active Zone Minutes', 'unit': 'min', 'min': 0, 'max': None, 'scale_max': 60,
                                      'targets': {'high': [22, 45], 'medium': [10, 22], 'low': [0, 10]}, 'over_margin': 15}}
        for i, (d, (day, seq)) in enumerate(list(cls.fitbit.days.items())):
            cls.fitbit.days[d] = (dict(day, readiness=None, load={'value': [30, 50, 5, 61][i], 'max': None, 'label': 'Active Zone Minutes',
                                                                  'scale_max': 60, 'unit': 'min'}), seq)
        # Polar: installed, not set up yet
        cls.polar = Connector('wizard-polar', 'Polar', 'polar', 'Training load', 0, 1)
        cls.polar.status = 'not_configured'
        cls.polar.days = {}
        cls.polar.days[END.isoformat()] = (make_day(END.isoformat(), 0), 1)
        cls.srv = {c.sid: serve(c.handler()) for c in (cls.whoop, cls.oura, cls.withings, cls.polar, cls.fitbit)}
        cls.url = {sid: f'http://127.0.0.1:{s.server_address[1]}' for sid, s in cls.srv.items()}

    @classmethod
    def tearDownClass(cls):
        S = cls.S
        S.GRANTS_FILE, S.ENV_SOURCES, S.DATA, S.KNOWN = cls.saved
        S._PROBE.update(at=0, list=None)
        for s in cls.srv.values():
            s.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fresh_box(self, grants, known_urls=None):
        """A clean Health copy with this grants file and the built-in list pointing at the fake connectors."""
        S = self.S
        S.DATA = self.tmp / ('data-' + str(len(list(self.tmp.iterdir()))))
        g = self.tmp / 'grants.json'
        g.write_text(json.dumps(grants))
        S.GRANTS_FILE, S.ENV_SOURCES = str(g), []
        urls = known_urls or {}
        S.KNOWN = [dict(k, base=urls.get(k['id'], 'http://127.0.0.1:9')) for k in self.saved[3]]
        S._PROBE.update(at=0, list=None)
        S.pull_all(force=True)

    def test_1_grants_list_every_provider(self):
        S = self.S
        # what an App Manager with provider_endpoints_all writes: Oura listed first, every provider named
        self.fresh_box({'outgoing': {'slot:health-data': 'x'}, 'incoming': [], 'enforce': False,
                        'provider_endpoints': {'slot:health-data': self.url['wizard-oura']},
                        'provider_endpoints_all': {'slot:health-data': {k: self.url[k] for k in ('wizard-oura', 'wizard-whoop', 'wizard-polar', 'wizard-withings')}}})
        info = S.sources_info()
        self.assertEqual([s['id'] for s in info['sources']], ['wizard-whoop', 'wizard-oura', 'wizard-polar', 'wizard-withings'])   # KNOWN order
        self.assertEqual(info['primary'], 'wizard-whoop')            # default: first connected in WHOOP, Oura... order
        self.assertTrue(info['choose_primary'])
        st = {s['id']: s for s in info['sources']}
        self.assertEqual(st['wizard-polar']['state'], 'not_set_up')
        self.assertFalse(st['wizard-polar']['connected'])
        self.assertEqual(st['wizard-polar']['days'], 0)              # nothing is read from a connector that is not set up
        self.assertEqual(st['wizard-polar']['app_path'], '/wizard-polar/')
        self.assertFalse(st['wizard-withings']['has_readiness'])
        self.assertTrue(st['wizard-whoop']['has_readiness'])
        self.assertEqual([c['id'] for c in info['connectors']], ['wizard-whoop', 'wizard-oura', 'wizard-fitbit', 'wizard-polar', 'wizard-withings'])
        self.assertFalse(next(c for c in info['connectors'] if c['id'] == 'wizard-fitbit')['installed'])

    def test_2_old_grants_known_list_fallback(self):
        S = self.S
        # an older App Manager: one endpoint per spot; the rest are found through the built-in list (ids + ports)
        self.fresh_box({'outgoing': {}, 'incoming': [], 'enforce': False, 'provider_endpoints': {'slot:health-data': self.url['wizard-whoop']}},
                       known_urls={'wizard-oura': self.url['wizard-oura']})
        self.assertEqual([s['id'] for s in S.sources_info()['sources']], ['wizard-whoop', 'wizard-oura'])

    def test_3_fill_switch_and_stale(self):
        S = self.S
        self.fresh_box({'outgoing': {}, 'incoming': [], 'enforce': False,
                        'provider_endpoints_all': {'slot:health-data': {k: self.url[k] for k in ('wizard-whoop', 'wizard-oura')}}})
        # per day: WHOOP's record; only the metrics it lacks come from Oura, marked with their origin
        d = S.days_list({'from': self.gap, 'to': self.gap})['days'][0]
        self.assertEqual(d['source'], 'wizard-whoop')
        self.assertEqual(d['origins']['sleep'], 'wizard-oura')
        self.assertEqual(d['origins']['steps'], 'wizard-oura')
        self.assertEqual(d['origins']['readiness'], 'wizard-whoop')
        self.assertEqual(d['origins']['load'], 'wizard-whoop')
        self.assertEqual(d['filled'], ['sleep', 'steps'])
        self.assertEqual(d['source_names'], {'wizard-whoop': 'WHOOP', 'wizard-oura': 'Oura'})
        self.assertEqual(d['sleep']['asleep_h'], 7.0)
        self.assertEqual(d['load']['max'], 21)                       # WHOOP's own load, not Oura's
        full = S.days_list({'from': (END - dt.timedelta(days=1)).isoformat(), 'to': (END - dt.timedelta(days=1)).isoformat()})['days'][0]
        self.assertEqual(full['filled'], [])
        # Oura's two older days (WHOOP has none) come whole from Oura
        old = S.days_list({'to': (END - dt.timedelta(days=10)).isoformat(), 'from': (END - dt.timedelta(days=11)).isoformat()})['days']
        self.assertEqual(len(old), 2)
        self.assertTrue(all(set(x['origins'].values()) == {'wizard-oura'} for x in old))
        t = S.daily_summary({'day': self.gap})
        self.assertEqual((t['source'], t['origins']['sleep'], t['filled']), ('wizard-whoop', 'wizard-oura', ['sleep', 'steps']))
        self.assertIn('sleep, steps from Oura', t['text'])
        self.assertEqual(t['sleep_band']['colour'], 'green')        # judged on the filled sleep
        # one source alone, on request: no fill
        self.assertIsNone(S.days_list({'source': 'wizard-whoop', 'from': self.gap, 'to': self.gap})['days'][0]['sleep'])
        # the owner switches the main source (saved server-side): Oura's records, gaps from WHOOP
        S.set_primary({'id': 'wizard-oura'})
        self.assertEqual(S._setting('primary'), 'wizard-oura')
        self.assertEqual(S.sources_info()['primary'], 'wizard-oura')
        d = S.days_list({'from': self.gap, 'to': self.gap})['days'][0]
        self.assertEqual((d['source'], d['origins']['sleep'], d['origins']['load'], d['filled']), ('wizard-oura', 'wizard-oura', 'wizard-oura', []))
        self.assertEqual(d['load']['max'], 100)
        self.assertEqual(S.daily_summary()['source_name'], 'Oura')
        code = S.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'health_daily_summary', 'arguments': {}}})
        self.assertEqual(code['result']['structuredContent']['source'], 'wizard-oura')
        S.set_primary({'id': 'auto'})
        self.assertIsNone(S._setting('primary'))
        self.assertEqual(S.sources_info()['primary'], 'wizard-whoop')
        # a connected source that has not synced for hours is shown as behind
        keep = self.oura.last_sync
        try:
            self.oura.last_sync = '2026-01-01T00:00:00.000Z'
            S._PROBE.update(at=0, list=None)
            self.assertEqual(next(s for s in S.sources_info()['sources'] if s['id'] == 'wizard-oura')['state'], 'stale')
            self.oura.last_sync = S._iso()
            S._PROBE.update(at=0, list=None)
            self.assertEqual(next(s for s in S.sources_info()['sources'] if s['id'] == 'wizard-oura')['state'], 'connected')
        finally:
            self.oura.last_sync = keep

    def test_4_no_readiness_source(self):
        S = self.S
        self.fresh_box({'outgoing': {}, 'incoming': [], 'enforce': False,
                        'provider_endpoints_all': {'slot:health-data': {k: self.url[k] for k in ('wizard-withings',)}}})
        self.assertEqual(S.sources_info()['primary'], 'wizard-withings')
        t = S.daily_summary()
        self.assertTrue(t['available'])
        self.assertIsNone(t['recovery_colour'])                       # grey: never a made-up score
        self.assertIsNone((t['readiness'] or {}).get('score'))
        self.assertEqual(t['no_readiness_from'], 'Withings')
        self.assertEqual((t['hrv_ms'], t['resting_hr_bpm']), (41.0, 58))   # heart numbers still there
        self.assertIsNone(t['activity_match'])
        self.assertIn('no recovery score from Withings', t['text'])
        # with WHOOP as well and Withings picked as main: WHOOP's score fills in, marked as WHOOP's
        self.fresh_box({'outgoing': {}, 'incoming': [], 'enforce': False,
                        'provider_endpoints_all': {'slot:health-data': {k: self.url[k] for k in ('wizard-withings', 'wizard-whoop')}}})
        S.set_primary({'id': 'wizard-withings'})
        t = S.daily_summary({'day': END.isoformat()})
        self.assertEqual(t['origins']['readiness'], 'wizard-whoop')
        self.assertEqual(t['origins']['hrv_ms'], 'wizard-withings')
        self.assertIsNone(t['no_readiness_from'])
        self.assertIsNotNone(t['recovery_colour'])

    def test_5_scale_aware_targets(self):
        S = self.S
        # WHOOP's 0-21 numbers stay exactly as before, with or without scale_max
        self.assertEqual({b: S.load_targets({'scale_max': 21})[b] for b in ('hi', 'md', 'lo', 'slack')},
                         {'hi': (14, 18), 'md': (10, 14), 'lo': (0, 10), 'slack': 3})
        for load in (None, {'max': 21}, {'scale_max': 21, 'max': 21, 'label': 'Strain'}):
            for v in [x / 2 for x in range(0, 43)]:
                for r in (0, 20, 33, 34, 50, 66, 67, 90):
                    a, b = S.activity_match(v, r, load), S.activity_match(v, r)
                    self.assertEqual((a or {}).get('colour'), (b or {}).get('colour'), (load, v, r))
                    self.assertEqual((a or {}).get('target'), (b or {}).get('target'))
        self.assertEqual(S.activity_match(5.8, 41, {'scale_max': 21})['target'], '10-14')
        self.assertEqual(S.activity_match(3, 20, {'scale_max': 21})['target'], 'under 10')
        self.assertEqual(S.activity_match(12, 50, {'scale_max': 21})['target_low'], 10)
        # another scale: the same fractions of its scale_max
        self.assertEqual(S.load_targets({'scale_max': 100})['md'], (48, 67))
        self.assertEqual(S.activity_match(55, 50, {'scale_max': 100})['colour'], 'green')
        self.assertEqual(S.activity_match(30, 50, {'scale_max': 100})['word'], 'Room to push')
        # explicit target hints from the source win
        hints = {'scale_max': 100, 'targets': {'high': [70, 90], 'medium': [40, 60], 'low': [0, 30]}}
        self.assertEqual(S.activity_match(65, 50, hints)['word'], 'A bit much')
        self.assertEqual(S.activity_match(50, 50, hints)['target'], '40-60')
        self.assertEqual(S.activity_match(50, 50, hints)['target_from'], 'source')
        # hints with no scale (zone minutes): slack from the medium band's width
        zm = {'label': 'Active zone minutes', 'unit': 'zone minutes', 'targets': {'high': [40, 60], 'medium': [20, 40], 'low': [0, 20]}}
        self.assertEqual(S.activity_match(55.1, 50, zm)['word'], 'Too much for today')   # over 40 + 0.75 x 20
        self.assertEqual(S.activity_match(50, 50, zm)['word'], 'A bit much')
        # no scale and no hints: no target, so no colour
        self.assertIsNone(S.activity_match(30, 50, {'label': 'Steps'}))

    def test_7_fitbit_shape(self):
        S = self.S
        # no readiness (available: false), open-ended load whose targets and margin live only in scales.load
        self.fresh_box({'outgoing': {}, 'incoming': [], 'enforce': False,
                        'provider_endpoints_all': {'slot:health-data': {'wizard-fitbit': self.url['wizard-fitbit']}}})
        self.assertFalse(S.sources_info()['sources'][0]['has_readiness'])
        days = S.days_list({})['days']
        self.assertEqual(days[0]['load']['targets'], {'high': [22, 45], 'medium': [10, 22], 'low': [0, 10]})   # from scales
        self.assertEqual(days[0]['load']['over_margin'], 15)
        self.assertTrue(all(d['activity_match'] is None for d in days))          # no recovery: grey, never invented
        self.assertEqual(S.daily_summary()['no_readiness_from'], 'Fitbit')
        # with WHOOP as main: Fitbit's own records still judged on Fitbit's targets when a day's load is filled from it
        ld = days[0]['load']
        self.assertEqual(S.activity_match(30, 80, ld)['word'], 'Right amount')    # Ready: 22-45
        self.assertEqual(S.activity_match(55, 80, ld)['word'], 'A bit much')      # up to 15 over
        self.assertEqual(S.activity_match(61, 80, ld)['word'], 'Too much for today')
        self.assertEqual(S.activity_match(15, 80, ld)['word'], 'Room to push')
        self.assertEqual(S.activity_match(15, 80, ld)['target'], '22-45')

    def test_6_scale_rules_match_screen(self):
        import shutil as sh
        import subprocess
        node = sh.which('node')
        if not node:
            self.skipTest('node is not installed')
        words = ROOT / 'web' / 'words.js'
        loads = [None, {'scale_max': 21}, {'max': 21}, {'scale_max': 100}, {'scale_max': 10},
                 {'scale_max': 100, 'targets': {'high': [70, 90], 'medium': [40, 60], 'low': [0, 30]}},
                 {'unit': 'zone minutes', 'targets': {'high': [40, 60], 'medium': [20, 40], 'low': [0, 20]}}, {'label': 'Steps'},
                 {'max': None, 'scale_max': 60, 'unit': 'min', 'targets': {'high': [22, 45], 'medium': [10, 22], 'low': [0, 10]}, 'over_margin': 15}]
        grid = [(v / 2, r, i) for i in range(len(loads)) for v in range(0, 201, 3) for r in (0, 20, 33, 34, 50, 66, 67, 90)]
        js = subprocess.run([node, '-e', f"const W=require({json.dumps(str(words))});const L={json.dumps(loads)};"
                             f"process.stdout.write(JSON.stringify({json.dumps(grid)}.map(([v,r,i])=>{{const m=W.actMatch(v,r,L[i]===null?undefined:L[i]);"
                             f"return m?[m.band,W.targetText(W.recBand(r),L[i]===null?undefined:L[i])]:null}})))"],
                            check=True, capture_output=True, text=True).stdout
        light = {'green': 'hi', 'yellow': 'md', 'red': 'lo'}
        py = []
        for v, r, i in grid:
            m = self.S.activity_match(v, r, loads[i])
            py.append([light[m['colour']], m['target'].replace('-', '\u2013')] if m else None)
        self.assertEqual(json.loads(js), py)


class PresenceTest(unittest.TestCase):
    """0.10.1: a source that is not set up / disconnected, or no longer discovered (uninstalled), stops counting at
    once; its stored days are purged only after 24 h off in a row (a restart never wipes anything); when it is back
    Health reads it again from the start. WHOOP's rows are never touched while WHOOP is connected."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = S = importlib.reload(server)
        cls.saved = (S.GRANTS_FILE, S.ENV_SOURCES, S.DATA, S.KNOWN)
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        S = cls.S
        S.GRANTS_FILE, S.ENV_SOURCES, S.DATA, S.KNOWN = cls.saved
        S._PROBE.update(at=0, list=None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        S = self.S
        self.whoop = Connector('wizard-whoop', 'WHOOP', 'whoop', 'Strain', 21, 10)
        self.oura = Connector('wizard-oura', 'Oura', 'oura', 'Activity', 100, 12)    # two days older than WHOOP's
        self.srv = {c.sid: serve(c.handler()) for c in (self.whoop, self.oura)}
        self.url = {sid: f'http://127.0.0.1:{s.server_address[1]}' for sid, s in self.srv.items()}
        S.DATA = self.tmp / ('data-' + self._testMethodName)
        self.grants = self.tmp / ('grants-' + self._testMethodName + '.json')
        S.GRANTS_FILE, S.ENV_SOURCES = str(self.grants), []
        S.KNOWN = [dict(k, base='http://127.0.0.1:9') for k in self.saved[3]]
        self.listed(['wizard-whoop', 'wizard-oura'])
        S.pull_all(force=True)

    def tearDown(self):
        for s in self.srv.values():
            s.shutdown(); s.server_close()

    def listed(self, ids):
        """What the App Manager grants: the providers installed right now."""
        self.grants.write_text(json.dumps({'provider_endpoints_all': {'slot:health-data': {i: self.url[i] for i in ids}}}))
        self.S._PROBE.update(at=0, list=None)

    def held(self, sid):
        c = self.S.db()
        try:
            return c.execute('SELECT COUNT(*) FROM days WHERE source=?', (sid,)).fetchone()[0]
        finally:
            c.close()

    def whoop_rows(self):
        c = self.S.db()
        try:
            return c.execute("SELECT date, data, pulled_at FROM days WHERE source='wizard-whoop' ORDER BY date").fetchall()
        finally:
            c.close()

    def age(self, sid, hours):
        """Pretend the source went off `hours` ago."""
        t = self.S._iso(self.S._now() - dt.timedelta(hours=hours))
        self.S._exec('UPDATE sources SET gone_since=? WHERE id=?', (t, sid))

    def src(self, sid):
        return next((x for x in self.S.sources_info()['sources'] if x['id'] == sid), None)

    def oldest(self):
        """The two oldest days only Oura has: the merged view shows them only while Oura counts."""
        return self.S.days_list({'from': (END - dt.timedelta(days=11)).isoformat(), 'to': (END - dt.timedelta(days=10)).isoformat()})['days']

    def test_1_disconnect(self):
        S = self.S
        S.set_primary({'id': 'wizard-oura'})
        self.assertEqual(S.daily_summary()['source'], 'wizard-oura')
        self.assertEqual(len(self.oldest()), 2)
        before = self.whoop_rows()
        for status in ('not_configured', 'disconnected'):
            self.oura.status = status
            S.pull_all(force=True)
            # stops counting at once: not the main source, nothing filled from it, its own read is empty
            self.assertEqual(S.daily_summary()['source'], 'wizard-whoop')
            self.assertEqual(self.oldest(), [])
            self.assertEqual(S.days_list({'source': 'wizard-oura'})['days'], [])
            self.assertTrue(all('wizard-oura' not in d['origins'].values() for d in S.days_list({})['days']))
            o = self.src('wizard-oura')
            self.assertEqual((o['days'], o['held_days'], o['installed']), (0, 12, True))
            self.assertIsNotNone(o['purge_at'])
            self.assertEqual(self.held('wizard-oura'), 12)            # kept for now
        self.assertEqual(S._setting('primary'), 'wizard-oura')        # the owner's pick is kept for when it is back
        self.age('wizard-oura', 23)
        S.pull_all(force=True)
        self.assertEqual(self.held('wizard-oura'), 12)                # under 24 h: still kept
        self.age('wizard-oura', 25)
        S.pull_all(force=True)
        self.assertEqual(self.held('wizard-oura'), 0)                 # purged
        self.assertEqual(self.src('wizard-oura')['state'], 'not_set_up')   # still listed: it is installed
        self.assertEqual(self.whoop_rows(), before)                   # WHOOP untouched throughout
        self.assertEqual(S.daily_summary()['source'], 'wizard-whoop')

    def test_2_uninstall(self):
        S = self.S
        S.set_primary({'id': 'wizard-oura'})
        before = self.whoop_rows()
        self.listed(['wizard-whoop'])                                  # Oura uninstalled
        self.srv['wizard-oura'].shutdown()
        S.pull_all(force=True)
        o = self.src('wizard-oura')
        self.assertEqual((o['installed'], o['state'], o['days'], o['held_days']), (False, 'not_installed', 0, 12))
        self.assertEqual(self.oldest(), [])
        self.assertEqual(S.daily_summary()['source'], 'wizard-whoop')
        self.age('wizard-oura', 25)
        S.pull_all(force=True)
        self.assertEqual(self.held('wizard-oura'), 0)
        self.assertIsNone(self.src('wizard-oura'))                     # forgotten entirely
        self.assertIsNone(S._setting('primary'))                       # its pick goes back to auto
        self.assertEqual(self.whoop_rows(), before)

    def test_3_brief_outage_keeps_data(self):
        S = self.S
        n = self.held('wizard-whoop')
        self.listed(['wizard-oura'])                                   # WHOOP restarting: not found for a moment
        S.pull_all(force=True)
        self.assertEqual(S.daily_summary()['source'], 'wizard-oura')   # its days are not used meanwhile
        self.assertEqual(self.held('wizard-whoop'), n)
        self.age('wizard-whoop', 2)
        S.pull_all(force=True)
        self.listed(['wizard-whoop', 'wizard-oura'])                   # back well inside 24 h
        self.whoop.calls.clear()
        S.pull_all()
        self.assertEqual(self.held('wizard-whoop'), n)                 # nothing was wiped
        self.assertEqual(S.daily_summary()['source'], 'wizard-whoop')
        self.assertIsNone(self.src('wizard-whoop')['off_since'])
        self.assertIn(('/api/days', {'changed_since': '', 'limit': '500'}), self.whoop.calls)   # read again from the start
        # Health's own restart: an off stamp from before is cleared by the first probe that finds the source
        self.age('wizard-whoop', 30)
        S._exec("UPDATE sources SET gone_since=NULL WHERE id='wizard-whoop'")
        S.pull_all(force=True)
        self.assertEqual(self.held('wizard-whoop'), n)

    def test_4_reconnect_repulls(self):
        S = self.S
        self.oura.status = 'not_configured'
        S.pull_all(force=True)
        self.age('wizard-oura', 25)
        S.pull_all(force=True)
        self.assertEqual(self.held('wizard-oura'), 0)
        self.oura.status = 'connected'                                 # set up again, same copy on the connector
        self.oura.calls.clear()
        S._PROBE.update(at=0, list=None)
        S.pull_all()
        self.assertEqual(self.held('wizard-oura'), 12)                 # everything read again, not just changes
        self.assertEqual(self.src('wizard-oura')['days'], 12)
        self.assertEqual(len(self.oldest()), 2)
        # reconnect inside 24 h: also read from the start
        self.oura.status = 'not_configured'
        S.pull_all(force=True)
        self.assertEqual(self.oldest(), [])
        self.oura.status = 'connected'
        self.oura.calls.clear()
        S._PROBE.update(at=0, list=None)
        S.pull_all()
        self.assertIn(('/api/days', {'changed_since': '', 'limit': '500'}), self.oura.calls)
        self.assertEqual(len(self.oldest()), 2)


class ViewTest(unittest.TestCase):
    """The one-screen view: picker, one focus card, the graph; every pop-up is the one sheet; band colours never use bare --danger."""
    web = Path(__file__).resolve().parent.parent / 'web'

    def test_one_screen(self):
        import re
        html = (self.web / 'index.html').read_text()
        js = (self.web / 'app.js').read_text()
        css = (self.web / 'styles.css').read_text()
        words = (self.web / 'words.js').read_text()
        # three panels (Recovery, Sleep, Activity), each a title, a coloured summary card and its own graph, cloned from one template
        for need in ('id="screen"', 'id="panels"', 'id="panel-tpl"', 'class="hl-cells"', 'class="hl-months"', 'id="legend"',
                     'id="metric"', 'id="dock"', 'data-m="recovery"', 'data-m="sleep"', 'data-m="activity"',
                     'class="hl-focus"', 'class="fnum"', 'fchip', 'class="hl-say fsay"', 'fkv', 'fstages', 'id="today"', 'id="when"', 'id="srcdot"',
                     'id="connect"', 'class="hl-tip"', '>Mon<', '>Wed<', '>Fri<', '>Back to today<'):
            self.assertIn(need, html)
        self.assertEqual(html.count('class="hl-panel"'), 3)
        tpl = html[html.index('id="panel-tpl"'):]
        self.assertLess(tpl.index('hl-title'), tpl.index('hl-focus'))
        # 0.8: no Why?/More sheet, no captions, no repeated day/Today inside the cards; the day shows once, only for a past day
        for gone in ('fmore', 'ftoday', 'fday', 'hl-cap', 'hl-ask', 'fbar', 'fscale', 'falso'):
            self.assertNotIn(gone, html + js, gone)
        for gone in ('openAbout', 'Why?', "'More'", 'Last 30 days'):
            self.assertNotIn(gone, js, gone)
        self.assertIn("el('when').hidden = !sel", js)
        # 0.11: narrow windows always open on Activity (first in the bottom bar); its card draws the target zone bar
        dock = html[html.index('id="metric"'):]
        self.assertLess(dock.index('data-m="activity"'), dock.index('data-m="recovery"'))
        self.assertIn('data-m="activity" aria-checked="true"', html)
        self.assertIn("let METRIC = 'activity'", js)
        self.assertNotIn('wizard-health.metric', js)
        for need in ('hl-abar fgoal', 'hl-azone', 'hl-amark', 'hl-aticks'):
            self.assertIn(need, html)
        self.assertIn('actGuide(', js)
        # the card wears its day's square class: same variable, so card fill == square fill
        self.assertIn("SCALE[m].cls(d)", js)
        self.assertRegex(css, r'\.hl-focus \{[^}]*background: var\(--c, var\(--cell0\)\)')
        self.assertIn('.hl-focus[data-on="canvas"]', css)
        self.assertLess(tpl.index('hl-focus'), tpl.index('hl-graph'))
        # 0.9: ONE legend for all three graphs, outside the cloned template, after the panels
        self.assertNotIn('hl-legend', tpl[:tpl.index('</template>')])
        self.assertNotIn('hl-foot', html + css)
        self.assertEqual(html.count('class="hl-legend"'), 1)
        self.assertLess(html.index('id="panels"'), html.index('id="legend"'))
        self.assertIn("['c-hi', 'Green', 'good'], ['c-md', 'Yellow', 'okay'], ['c-lo', 'Red', 'needs attention'], ['', '', 'No data']", js)
        # 0.9: one traffic light: every metric wears only c-hi/c-md/c-lo (no green/blue intensity steps)
        self.assertNotRegex(css + js, r"[.' ]c-(?:g|a|[1-5])\b")
        for gone in ('--blu', '--hue', 'sleepLevel', 'actLevel', 'ACT_SAY'):
            self.assertNotIn(gone, css + js + words, gone)
        self.assertIn("const LIGHT = { hi: 'c-hi', md: 'c-md', lo: 'c-lo' }", js)
        self.assertNotRegex(tpl[:tpl.index('</template>')], r'\sid="')          # cloned three times: no ids inside
        # narrow windows: the picker is a bottom bar after the scrolling content, never inside it
        self.assertLess(html.index('</main>'), html.index('id="dock"'))
        self.assertLess(html.index('id="panels"'), html.index('id="metric"'))
        # wide = container width (never the device), the same breakpoint in CSS and JS, all three at once with no picker
        self.assertIn('const WIDE = 680', js)
        self.assertIn('@container wz-app (min-width: 680px)', css)
        self.assertIn('@container wz-app (max-width: 679.98px)', css)
        self.assertRegex(css, r'@container wz-app \(min-width: 680px\) \{\s*\.hl-dock \{ display: none; \}')
        # 0.14: one row sized by importance: Activity half, Recovery and Sleep a quarter each
        self.assertIn('grid-template-columns: minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1fr)', css)
        # phones: two tabs, Activity and Recovery & sleep (Sleep stacked under Recovery)
        dock2 = html[html.index('id="metric"'):html.index('</nav>')]
        self.assertEqual(dock2.count('role="radio"'), 2)
        self.assertIn('>Recovery &amp; sleep<', dock2)
        self.assertIn("recovery: ['recovery', 'sleep']", js)
        self.assertLess(html.index('data-m="activity" aria-label'), html.index('data-m="recovery" aria-label'))
        self.assertIn('grid-template-rows: subgrid', css)
        self.assertNotIn('@media (min-width', css)
        # one picked day for all three: pick() re-renders every panel; side-by-side graphs scroll together
        self.assertIn('for (const m of METRICS) focusPanel(P[m], iso)', js)
        self.assertIn('function syncScroll', js)
        # the bottom bar carries the safe-area/full-screen clearance
        self.assertRegex(css, r'\.hl-dock \{[^}]*var\(--hl-below\)')
        # plain words for the bands (WHOOP recovery 0-33/34-66/67-100; strain <10/10-14/14-18/18+; sleep vs need 85%/70%)
        self.assertIn('src="words.js"', html)
        self.assertLess(html.index('src="words.js"'), html.index('src="app.js"'))
        for word in ("'Ready'", "'So-so'", "'Run down'", "'High'", "'Light'", "'Moderate'", "'All out'", "'Enough'", "'A bit short'", "'Short'",
                     'needed', 'less than you needed', 'Partly recovered', 'Not recovered', 'Well recovered', 'Right amount', 'Room to push',
                     'A bit much', 'Too much for today', 'No recovery data', 'out of ', 'Target for today', 'from your recovery',
                     'Back to today', 'usual', 'Resting heart rate', 'Blood oxygen', 'Skin temperature', 'Calories', 'Steps', 'Workouts',
                     'Heart rate variability', 'higher = more recovered', 'lower is better', 'Time in bed', 'Dreaming (REM)', 'Deep',
                     'did you get enough', 'did it match your recovery', 'Measured from your heart overnight, so it can differ from your sleep.'):
            self.assertIn(word, js + html + words, word)
        # 0.10: several sources: per-source states with Set up links to the connector's own app, the main-source choice,
        # the tiny origin note of a filled metric, and an honest no-score for a source without readiness
        for word in ("'Set up'", "'Make main'", "'Main'", "'No score from '", "' does not give a recovery score.'", "'from ' + srcName(o)",
                     "'Behind'", "'Not set up'", "'Set up ' + c.name", "api('api/primary', { id })", 'class="hl-from ffrom"', 'id="setups"'):
            self.assertIn(word, js + html, word)
        self.assertIn('actMatch(act(d), recov(d), loadOf(d))', js)          # targets on the source's own scale
        self.assertIn("'out of ' + mx", js)
        self.assertIn('v >= 67', words); self.assertIn('v >= 34', words)
        self.assertIn('v >= 18', words); self.assertIn('v >= 14', words); self.assertIn('v >= 10', words)
        self.assertIn('0.85', words); self.assertIn('0.70', words)
        # no bare jargon on the screen or in sheets: HRV only beside its plain name; no strain/efficiency/performance words
        code = re.sub(r'(?m)(^|\s)//.*$', '', js + words)   # comments may say what WHOOP calls things
        shown = re.findall(r"'([^'\\]*(?:\\.[^'\\]*)*)'", code)
        for t in shown:
            if 'HRV' in t:
                self.assertIn('Heart rate variability (HRV)', t)
            for bad in ('RHR', 'SpO2', 'Strain', 'strain', 'Efficiency', 'efficiency', 'performance', 'Performance', 'alcool', "'Low'", "'Medium'"):
                self.assertNotIn(bad, t, t)
        # thumb-sized squares on a phone
        self.assertRegex(js, r'narrow \? 22')
        self.assertIn('WizardApp.back.register', js)
        self.assertIn('WzSheet.open', js)
        self.assertIn("api('api/summary')", js)
        self.assertNotIn('role="tablist"', html)
        self.assertEqual(html.count('<dialog'), 1)
        self.assertIn('class="wz-sheet', html)
        # flat GitHub squares: small radius, no gradients, glow or dithering
        self.assertRegex(css, r'\.hl-cell \{[^}]*border-radius: [2-4]px')
        for gone in ('gradient', 'drop-shadow', 'image-rendering', 'animation: cd-'):
            self.assertNotIn(gone, css)
        self.assertNotRegex(css, r'#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(')
        bands = re.findall(r'--(?:hi|md|lo|blu|grn):\s*([^;]+);', css)
        self.assertGreaterEqual(len(bands), 5)
        for v in bands:
            self.assertNotIn('var(--danger)', v)


class LightsTest(unittest.TestCase):
    """The server's traffic-light fields (summary, days, MCP) follow the same rules as the screen."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
        import server
        cls.S = server

    def test_sleep_band(self):
        sb = self.S.sleep_band
        self.assertEqual([sb(h, 10)['colour'] for h in (8.5, 8.49, 7.0, 6.99)], ['green', 'yellow', 'yellow', 'red'])
        self.assertEqual(sb(5.13, 9.52), {'colour': 'red', 'word': 'Short', 'pct_of_need': 54, 'by': 'need'})
        self.assertEqual([sb(h, None)['colour'] for h in (7, 6.5, 5.9)], ['green', 'yellow', 'red'])
        self.assertEqual(sb(6.5, 0)['by'], 'hours')
        self.assertIsNone(sb(None, 9))

    def test_activity_match(self):
        am = self.S.activity_match
        c = lambda v, r: (am(v, r) or {}).get('colour')
        self.assertEqual([c(v, 80) for v in (2, 13.9, 14, 18, 18.1, 21)], ['yellow', 'yellow', 'green', 'green', 'yellow', 'yellow'])
        self.assertEqual([c(v, 41) for v in (5.8, 9.9, 10, 14, 17, 17.1)], ['yellow', 'yellow', 'green', 'green', 'yellow', 'red'])
        self.assertEqual([c(v, 20) for v in (0, 10, 10.1, 13, 13.1)], ['green', 'green', 'yellow', 'yellow', 'red'])
        self.assertEqual(am(5.8, 41)['word'], 'Room to push')
        self.assertEqual(am(5.8, 41)['target'], '10-14')
        self.assertEqual(am(19, 20)['word'], 'Too much for today')
        self.assertEqual(am(3, 20)['target'], 'under 10')
        self.assertIsNone(am(10, None)); self.assertIsNone(am(None, 50))

    def test_lights_match_screen(self):
        # the Python and JS rules agree on a grid of values
        import shutil as sh
        import subprocess
        node = sh.which('node')
        if not node:
            self.skipTest('node is not installed')
        words = ROOT / 'web' / 'words.js'
        grid = [(v / 2, r) for v in range(0, 43) for r in (0, 20, 33, 34, 50, 66, 67, 90)]
        js = subprocess.run([node, '-e', f"const W=require({json.dumps(str(words))});process.stdout.write(JSON.stringify("
                             f"{json.dumps(grid)}.map(([v,r])=>(W.actMatch(v,r)||{{}}).band||null)))"], check=True, capture_output=True, text=True).stdout
        py = [{'green': 'hi', 'yellow': 'md', 'red': 'lo'}.get((self.S.activity_match(v, r) or {}).get('colour')) for v, r in grid]
        self.assertEqual(json.loads(js), py)


class IconTest(unittest.TestCase):
    """The app ships its own icon: a full-colour square for the store (icon.png from wizard.md) and a one-colour
    silhouette at web/icon.svg (served at /wizard-health/icon.svg) that the shell tints like its own sigils."""
    root = Path(__file__).resolve().parent.parent

    def test_icons(self):
        import re
        w = (self.root / 'wizard.md').read_text()
        self.assertIn('\nicon: icon.png\n', w)
        png = (self.root / 'icon.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(int.from_bytes(png[16:20], 'big'), 1024)
        self.assertEqual(int.from_bytes(png[20:24], 'big'), 1024)
        mask = (self.root / 'web' / 'icon.svg').read_text()
        self.assertIn('viewBox="0 0 512 512"', mask)
        self.assertNotRegex(mask, r'fill="#|<text|<image|href=')          # a pure silhouette: the shell paints it
        self.assertEqual(mask.count('<rect'), 3)                           # three days stepping up, like the graph


class WordsTest(unittest.TestCase):
    """The plain words and sentences (web/words.js), run in node exactly as the screen runs them."""
    words = Path(__file__).resolve().parent.parent / 'web' / 'words.js'

    def run_js(self, expr):
        import json
        import shutil as sh
        import subprocess
        node = sh.which('node')
        if not node:
            self.skipTest('node is not installed')
        code = f"const W = require({json.dumps(str(self.words))}); process.stdout.write(JSON.stringify({expr}));"
        return json.loads(subprocess.run([node, '-e', code], check=True, capture_output=True, text=True).stdout)

    def test_titles(self):
        # the title says what each metric is; exactly one line (Recovery's) explains recovery vs sleep
        self.assertEqual(self.run_js("W.title('recovery')"),
                         {'name': 'Recovery', 'gloss': 'how ready your body is', 'sub': 'Measured from your heart overnight, so it can differ from your sleep.'})
        self.assertEqual(self.run_js("W.title('sleep').sub"), '')
        self.assertEqual(self.run_js("W.title('activity').sub"), '')
        self.assertEqual(self.run_js("W.title('sleep').gloss"), 'did you get enough')
        self.assertEqual(self.run_js("W.title('activity').gloss"), 'did it match your recovery')

    def test_summaries(self):
        self.assertEqual(self.run_js("W.REC_SAY.now.md"), 'Partly recovered. A normal day is fine.')
        self.assertEqual(self.run_js("W.sleepSay(308, 571)"), '4h 23m less than you needed.')
        self.assertEqual(self.run_js("W.sleepSay(40, 70)"), '30m less than you needed.')
        self.assertEqual(self.run_js("W.sleepSay(500, 480)"), 'All the sleep you needed.')
        # 0.12: with the need's parts, say which part was missed (the real 2 Nov night: 6h 20m of 9h 43m, usual 7h 36m)
        self.assertEqual(self.run_js("W.sleepSay(380, 583, 456)"), '3h 23m short: 1h 16m under a normal night, plus 2h 7m to catch up from earlier nights.')
        self.assertEqual(self.run_js("W.sleepSay(470, 583, 456)"), 'A normal night, but you are still 1h 53m behind from earlier short nights.')
        self.assertEqual(self.run_js("W.sleepSay(400, 466, 456)"), '1h 6m less than you needed.')      # catch-up under 15m: plain
        self.assertEqual(self.run_js("W.sleepSay(600, 583, 456)"), 'All the sleep you needed.')
        self.assertEqual(self.run_js("W.needGloss(583, 456)"), 'usual 7h 36m + 2h 7m catch-up')
        self.assertEqual(self.run_js("W.needGloss(460, 456)"), 'your usual night')
        self.assertEqual(self.run_js("W.needGloss(583, null)"), '')
        for say in self.run_js("[...Object.values(W.REC_SAY.now), ...Object.values(W.REC_SAY.past), ...Object.values(W.MATCH_SAY.now), ...Object.values(W.MATCH_SAY.past)]"):
            self.assertLessEqual(len(say), 45, say)

    def test_contrast(self):
        self.assertAlmostEqual(self.run_js("W.contrast([0,0,0],[255,255,255])"), 21, places=3)
        self.assertEqual(self.run_js("W.betterInk([240,240,240],[20,20,20],[250,250,250])"), 'a')
        self.assertEqual(self.run_js("W.betterInk([30,30,40],[20,20,20],[250,250,250])"), 'b')

    def test_chip_words(self):
        self.assertEqual(self.run_js("[0, 33, 34, 66, 67, 100].map(v => W.REC_WORD[W.recBand(v)])"),
                         ['Run down', 'Run down', 'So-so', 'So-so', 'Ready', 'Ready'])
        self.assertEqual(self.run_js("[[8.5, 9], [7, 9], [5, 9]].map(([h, n]) => W.SLEEP_WORD[W.sleepBand(h, n)])"),
                         ['Enough', 'A bit short', 'Short'])

    def test_sleep_band_edges(self):
        # by percent of need: 85% and over green, 70-85% yellow, under 70% red
        B = lambda h, n: self.run_js(f"W.sleepBand({h}, {n})")
        self.assertEqual(B(8.5, 10), 'hi'); self.assertEqual(B(8.49, 10), 'md')
        self.assertEqual(B(7.0, 10), 'md'); self.assertEqual(B(6.99, 10), 'lo')
        self.assertEqual(B(5.13, 9.52), 'lo')                       # the real 5h 8m of 9h 31m night: 54%
        self.assertEqual(B(10, 9), 'hi')
        # no need: by hours (7h+ green, 6-7h yellow, under 6h red)
        for n in ('null', 0, 'undefined'):
            self.assertEqual([B(h, n) for h in (7, 6.99, 6, 5.99)], ['hi', 'md', 'md', 'lo'], n)
        self.assertIsNone(B('null', 9))                               # no sleep: grey

    def test_activity_match_edges(self):
        M = lambda v, r: self.run_js(f"W.actMatch({v}, {r})")
        band = lambda v, r: (M(v, r) or {}).get('band')
        word = lambda v, r: (M(v, r) or {}).get('word')
        # Ready (target 14-18): under -> yellow Room to push (never red, even a very light day), in -> green, over -> yellow only
        self.assertEqual([band(v, 80) for v in (0, 5, 10.9, 11, 13.9, 14, 18, 18.1, 21)], ['md', 'md', 'md', 'md', 'md', 'hi', 'hi', 'md', 'md'])
        self.assertEqual(word(2, 80), 'Room to push'); self.assertEqual(word(19, 80), 'A bit much'); self.assertEqual(word(16, 80), 'Right amount')
        # So-so (target 10-14): under -> yellow, in -> green, up to 17 -> yellow A bit much, over 17 -> red
        self.assertEqual([band(v, 41) for v in (5.8, 6.9, 7, 9.9, 10, 14, 14.1, 17, 17.1, 21)], ['md', 'md', 'md', 'md', 'hi', 'hi', 'md', 'md', 'lo', 'lo'])
        self.assertEqual(word(5.8, 41), 'Room to push'); self.assertEqual(word(15, 41), 'A bit much'); self.assertEqual(word(18, 41), 'Too much for today')
        # Run down (target under 10): any rest is green, 10-13 yellow, over 13 red (High or All out)
        self.assertEqual([band(v, 20) for v in (0, 4, 10, 10.1, 13, 13.1, 15, 19)], ['hi', 'hi', 'hi', 'md', 'md', 'lo', 'lo', 'lo'])
        self.assertEqual(word(1, 20), 'Right amount'); self.assertEqual(word(12, 20), 'A bit much'); self.assertEqual(word(19, 20), 'Too much for today')
        # recovery band edges pick the target: 33 Run down, 34 So-so, 66 So-so, 67 Ready
        self.assertEqual([band(12, r) for r in (33, 34, 66, 67)], ['md', 'hi', 'hi', 'md'])
        # rounded as shown: 17.04 shows as 17.0, so yellow on a So-so day
        self.assertEqual(band(17.04, 50), 'md'); self.assertEqual(band(17.06, 50), 'lo')
        # missing recovery or strain: no colour (grey)
        for args in ((10, 'null'), ('null', 50), (10, 'undefined')):
            self.assertIsNone(M(*args), args)
        # the plain sentences
        S = lambda v, r, past: self.run_js(f"W.matchSay(W.actMatch({v}, {r}), {r}, {str(past).lower()})")
        self.assertEqual(S(12, 50, True), 'Right amount for how recovered you were.')
        self.assertEqual(S(18, 20, True), 'More than your body was ready for.')
        self.assertEqual(S(5, 80, True), 'You had room to push harder.')
        self.assertEqual(S(16, 50, False), 'A little more than your body is ready for.')
        self.assertEqual(S(3, 20, False), 'Taking it easy is right today.')
        self.assertEqual(self.run_js("[W.targetText('hi'), W.targetText('md'), W.targetText('lo')]"), ['14\u201318', '10\u201314', 'under 10'])

    def test_activity_guide(self):
        # 0.11: the Activity bar + direction line, so a yellow card says which way to go
        G = lambda v, r, past=False: self.run_js(f"W.actGuide({v}, {r}, undefined, {str(past).lower()})")
        g = G(2.5, 70)                                                # the real 2 Nov morning: Ready, 2.5 of 14-18
        self.assertEqual((g['lo'], g['hi'], g['max'], g['v'], g['side']), (14, 18, 21, 2.5, 'under'))
        self.assertEqual(g['text'], '11.5 to go to reach your target.')
        self.assertEqual(G(2.5, 70, True)['text'], '11.5 short of your target.')
        self.assertEqual(G(12, 50)['text'], 'In your target. Fine to stop, or go up to 14.')
        self.assertEqual(G(14, 50)['text'], 'Top of your target. Stop here.')
        self.assertEqual(G(15.5, 50)['text'], '1.5 over your target. Take it easy.')
        self.assertEqual(G(18, 50)['text'], '4 over your target. Stop for today.')
        self.assertEqual(G(18, 50, True)['text'], '4 over your target.')
        r = G(4, 20)                                                  # Run down: the zone runs from 0, rest is right
        self.assertEqual((r['lo'], r['hi'], r['text']), (0, 10, 'Rest is right today. Stay under 10.'))
        self.assertIsNone(G(5, 'null'))                               # no recovery: no bar
        # the direction always agrees with the colour's side
        for v in (0, 9.9, 10, 14, 14.1, 17.1, 21):
            self.assertEqual(G(v, 50)['side'], self.run_js(f"W.actMatch({v}, 50).side"), v)

    def test_recovery_vs_sleep(self):
        R = lambda score, h, need, past=False: self.run_js(f"W.recoveryVsSleep({score}, {h}, {need}, {str(past).lower()})")
        # short sleep (under 70% of need), heart held up
        self.assertEqual(R(80, 5, 9), 'Short sleep, but your heart still looks rested.')
        self.assertEqual(R(41, 5.13, 9.52), 'Your heart coped, but short sleep is holding this back.')   # a real short night
        self.assertEqual(R(41, 5.13, 9.52, True), 'Your heart coped, but short sleep held this back.')
        self.assertEqual(R(80, 5, 9, True), 'Short sleep, but your heart still looks rested.')
        # enough sleep, heart says otherwise
        low = R(20, 8.5, 9)
        self.assertTrue(low.startswith('Enough sleep, so something else'), low)
        self.assertIn('alcohol', low)
        self.assertIn('is weighing on you', low)
        self.assertIn('was weighing on you', R(20, 8.5, 9, True))
        self.assertIn('may be holding you back', R(50, 8.5, 9))
        self.assertIn('may have held you back', R(50, 8.5, 9, True))
        # sleep and recovery agree, or a value is missing: no extra sentence
        for args in ((20, 5, 9), (80, 8.5, 9), (80, 7, 9), (20, 7, 9), ('null', 5, 9), (80, 'null', 9), (80, 5, 'null')):
            self.assertEqual(R(*args), '', args)


if __name__ == '__main__':
    unittest.main()
