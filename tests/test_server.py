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
                                            'scales': {'readiness': {'label': 'Recovery' if me.provider == 'whoop' else 'Readiness', 'min': 0, 'max': 100},
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
        self.assertEqual((o['installed'], o['status'], o['days']), (False, 'not_installed', 3))
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
        finally:
            S.GRANTS_FILE, S.ENV_SOURCES, S.DATA = saved
            S._PROBE.update(at=0, list=None)
            shutil.rmtree(tmp, ignore_errors=True)


class ViewTest(unittest.TestCase):
    """The one-screen view: picker, one focus card, the graph; every pop-up is the one sheet; band colours never use bare --danger."""
    web = Path(__file__).resolve().parent.parent / 'web'

    def test_one_screen(self):
        import re
        html = (self.web / 'index.html').read_text()
        js = (self.web / 'app.js').read_text()
        css = (self.web / 'styles.css').read_text()
        # three panels (Recovery, Sleep, Activity), each a question, a focus card and its own graph, cloned from one template
        for need in ('id="screen"', 'id="panels"', 'id="panel-tpl"', 'class="hl-cells"', 'class="hl-months"', 'class="hl-legend"', 'class="hl-cap"',
                     'id="metric"', 'id="dock"', 'data-m="recovery"', 'data-m="sleep"', 'data-m="activity"',
                     'class="hl-focus"', 'class="fnum"', 'fchip', 'class="hl-say fsay"', 'fmore', 'fbar', 'id="today"', 'ftoday', 'id="srcdot"',
                     'id="connect"', 'class="hl-tip"', '>Mon<', '>Wed<', '>Fri<'):
            self.assertIn(need, html)
        self.assertEqual(html.count('class="hl-panel"'), 3)
        tpl = html[html.index('id="panel-tpl"'):]
        self.assertLess(tpl.index('hl-ask'), tpl.index('hl-focus'))
        self.assertLess(tpl.index('hl-focus'), tpl.index('hl-graph'))
        self.assertLess(tpl.index('hl-cells'), tpl.index('hl-legend'))
        self.assertNotRegex(tpl[:tpl.index('</template>')], r'\sid="')          # cloned three times: no ids inside
        # narrow windows: the picker is a bottom bar after the scrolling content, never inside it
        self.assertLess(html.index('</main>'), html.index('id="dock"'))
        self.assertLess(html.index('id="panels"'), html.index('id="metric"'))
        # wide = container width (never the device), the same breakpoint in CSS and JS, all three at once with no picker
        self.assertIn('const WIDE = 680', js)
        self.assertIn('@container wz-app (min-width: 680px)', css)
        self.assertIn('@container wz-app (max-width: 679.98px)', css)
        self.assertRegex(css, r'@container wz-app \(min-width: 680px\) \{\s*\.hl-dock \{ display: none; \}')
        self.assertIn('grid-template-columns: repeat(3, minmax(0, 1fr))', css)
        self.assertIn('grid-template-rows: subgrid', css)
        self.assertNotIn('@media (min-width', css)
        # one picked day for all three: pick() re-renders every panel; side-by-side graphs scroll together
        self.assertIn('for (const m of METRICS) focusPanel(P[m], iso)', js)
        self.assertIn('function syncScroll', js)
        # the bottom bar carries the safe-area/full-screen clearance
        self.assertRegex(css, r'\.hl-dock \{[^}]*var\(--hl-below\)')
        # plain words for the bands (WHOOP recovery 0-33/34-66/67-100; strain <10/10-14/14-18/18+; sleep vs need 85%/70%)
        words = (self.web / 'words.js').read_text()
        self.assertIn('src="words.js"', html)
        self.assertLess(html.index('src="words.js"'), html.index('src="app.js"'))
        for word in ("'Ready'", "'So-so'", "'Run down'", "'High'", "'Light'", "'Moderate'", "'All out'", "'Enough'", "'A bit short'", "'Short'",
                     'needed', 'less than your body needed', 'partly recovered', "hasn't recovered", 'well recovered', "didn't push hard",
                     'Why?', "'More'", 'Back to today', 'usual', 'Resting heart rate', 'Blood oxygen', 'Skin temperature', 'Calories',
                     'Heart rate variability (HRV)', 'Higher usually means more recovered', 'Lower is usually better',
                     "'Less sleep', 'More sleep'", "'Easier', 'Harder'", "'Run down', 'Ready'", 'Green = ready, yellow = so-so, red = run down',
                     "(dark ? 'Brighter' : 'Darker') + ' = more sleep.'", "(dark ? 'Brighter' : 'Darker') + ' = harder day.'", 'data-color-scheme', 'out of ', '0 = resting all day', 'ask1', 'falso', 'fscale'):
            self.assertIn(word, js + html + words, word)
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

    def test_questions(self):
        self.assertEqual(self.run_js("W.ask('recovery', false)"),
                         {'q': 'How ready is your body today?', 'sub': 'Measured from your heart overnight, not just your sleep.'})
        self.assertEqual(self.run_js("W.ask('sleep', false)"), {'q': 'Did you sleep enough last night?', 'sub': ''})
        self.assertEqual(self.run_js("W.ask('activity', false)"), {'q': 'How hard did you push your body today?', 'sub': ''})
        self.assertEqual(self.run_js("W.ask('recovery', true).q"), 'How ready was your body that day?')

    def test_chip_words(self):
        self.assertEqual(self.run_js("[0, 33, 34, 66, 67, 100].map(v => W.REC_WORD[W.recBand(v)])"),
                         ['Run down', 'Run down', 'So-so', 'So-so', 'Ready', 'Ready'])
        self.assertEqual(self.run_js("[[8.5, 9], [7, 9], [5, 9]].map(([h, n]) => W.SLEEP_WORD[W.sleepBand(h, n)])"),
                         ['Enough', 'A bit short', 'Short'])

    def test_recovery_vs_sleep(self):
        R = lambda score, h, need, past=False: self.run_js(f"W.recoveryVsSleep({score}, {h}, {need}, {str(past).lower()})")
        # short sleep (under 70% of need), heart held up
        self.assertEqual(R(80, 5, 9), 'You slept short, but your heart shows you handled it.')
        self.assertEqual(R(41, 5.13, 9.52), 'You slept short, but your heart shows you held up okay.')   # a real short night
        self.assertEqual(R(80, 5, 9, True), 'You slept short, but your heart showed you handled it.')
        # enough sleep, heart says otherwise
        low = R(20, 8.5, 9)
        self.assertTrue(low.startswith('You slept enough, but something else'), low)
        self.assertIn('alcohol', low)
        self.assertIn('is weighing on you', low)
        self.assertIn('was weighing on you', R(20, 8.5, 9, True))
        self.assertIn('may be holding you back', R(50, 8.5, 9))
        # sleep and recovery agree, or a value is missing: no extra sentence
        for args in ((20, 5, 9), (80, 8.5, 9), (80, 7, 9), (20, 7, 9), ('null', 5, 9), (80, 'null', 9), (80, 5, 'null')):
            self.assertEqual(R(*args), '', args)


if __name__ == '__main__':
    unittest.main()
