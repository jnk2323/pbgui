"""PB8 date shortcuts through real menu, editor, date inputs and local assets."""

import copy
import functools
import traceback
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi import Request
from api.page_templates import render_page_urls
from api import backtest_v8
from api.auth import require_auth
from fastapi import FastAPI
from fastapi.testclient import TestClient
from browser_driver import sync_browser

ROOT = Path(__file__).resolve().parents[2]


def _concise(function):
    """Keep bounded controller diagnostics from hiding the actual assertion."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pytest.fail(traceback.format_exc()[-1400:], pytrace=False)
    return wrapped


@pytest.fixture
def date_page(request, monkeypatch):
    """Intercept all requests and use only disposable editor and candle results."""
    version = getattr(request, 'param', 'v8')
    prefix = '/mounted'
    html = render_page_urls(Request({'type': 'http', 'root_path': prefix}),
                            (ROOT / 'frontend/v7_backtest.html').read_text(), '/api/backtest-' + version)
    for key, value in {'VERSION': 'test', 'SERIAL': '1', 'NAV_HASH': 'test',
                       'BACKTEST_VERSION': version, 'BACKTEST_LABEL': 'PB' + version}.items():
        html = html.replace('%%' + key + '%%', value)
    config = json.loads((ROOT / 'tests/fixtures/pb8_october_template.json').read_text())['config']
    config['backtest'].update(exchanges=['bybit'], start_date='2020-01-01', end_date='now')
    config['live'].update(approved_coins={'long': ['BTC'], 'short': []}, ignored_coins={'long': [], 'short': []})
    config['backtest']['suite_enabled'] = True
    config['backtest']['scenarios'] = [{'label': 'kept', 'start_date': '2022-01-01', 'end_date': '2022-02-01'}]
    calls, errors, held = [], [], []
    control = {'hold': False, 'status': 'completed', 'result': {
        'earliest': {'available': True, 'start_date': '2024-01-02'},
        'all_markets': {'available': True, 'start_date': '2024-03-01'},
        'known_pair_count': 1, 'pair_count': 1}}
    app = FastAPI()
    app.include_router(backtest_v8.router, prefix='/api/backtest-v8')
    app.dependency_overrides[require_auth] = lambda: None
    from api import optimize_v8

    def start_lookup(config):
        """Exercise real HTTP/body validation while replacing only the runtime job."""
        return {'job_id': 'abcdef123456', 'status': 'queued'}

    monkeypatch.setattr(optimize_v8, 'start_pb8_ohlcv_start_date_job', start_lookup)
    client = TestClient(app)
    with sync_browser() as driver:
        browser = driver.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.set_default_timeout(7000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script('''window.WebSocket=class {constructor(){this.readyState=1;}close(){}send(){}};''')
        def respond(route):
            """Serve versioned assets and deterministic API fixtures, never PBGui."""
            path = urlparse(route.request.url).path.removeprefix(prefix)
            calls.append((route.request.method, path))
            if path.startswith('/app/'):
                asset = ROOT / 'frontend' / path.removeprefix('/app/')
                if asset.is_file():
                    route.fulfill(body=asset.read_bytes(), content_type={'.js':'text/javascript', '.css':'text/css'}.get(asset.suffix, 'application/octet-stream'))
                else:
                    route.fulfill(status=404, body='')
            elif path == '/start':
                route.fulfill(body='<nav id="topnav"></nav><script>window.PBGUI_NAV_CONFIG={authenticated:true,apiBase:"/mounted/api/balance-calc",current:"info_balance_calc"};</script><script src="/mounted/app/pbgui_nav.js?v=test"></script>', content_type='text/html')
            elif path == '/api/backtest-' + version + '/main_page':
                route.fulfill(body=html, content_type='text/html')
            elif path.endswith('/configs/new-config'):
                route.fulfill(json={'config': config})
            elif path.endswith('/configs'):
                route.fulfill(json={'configs': []})
            elif path.endswith('/settings'):
                route.fulfill(json={'cpu':1, 'cpu_max':2, 'exchange_options':['bybit'], 'use_pbgui_market_data':False})
            elif path.endswith('/coins/status') or path.endswith('/symbols'):
                route.fulfill(json={'symbols':['BTC'], 'catalog':[], 'statuses':{}})
            elif path.endswith('/ohlcv-start-dates'):
                if control['hold']:
                    held.append(route)
                elif control.get('http_error'):
                    route.fulfill(status=422, json={'detail': control['http_error']})
                else:
                    # Forward the browser's actual headers/body to FastAPI. A missing
                    # JSON Content-Type must fail here, just as on the running API.
                    headers = {name.lower(): value for name, value in route.request.headers.items()}
                    response = client.post(path, content=route.request.post_data,
                                           headers={'Content-Type': headers.get('content-type', '')})
                    route.fulfill(status=response.status_code, body=response.content,
                                  content_type='application/json')
            elif '/ohlcv-start-dates/' in path:
                route.fulfill(json={'status': control['status'], 'result': {'start_date_options':control['result']}})
            elif path == '/api/help/index':
                route.fulfill(json=[{'file':'42_pbv8_backtest.md', 'title':'PBv8 Backtest'}])
            elif path == '/api/help/content':
                route.fulfill(json={'content':(ROOT / 'docs/help/42_pbv8_backtest.md').read_text()})
            elif path.endswith('/stream'):
                route.fulfill(status=204, body='')
            else:
                route.fulfill(json={})
        page.route('**/*', respond)
        page.goto('http://backtest.test' + prefix + '/start')
        page.locator('.nav-group-btn[data-group="pb' + version + '"]').click()
        page.locator('.nav-item[data-page="' + version + '_backtest"]').click()
        try:
            page.wait_for_function('window._backtestStartDateController && window.backtestShell')
        except Exception as exc:
            pytest.fail(str(exc) + ' page errors: ' + repr(errors), pytrace=False)
        page.locator('[onclick="newConfig()"]').click()
        try:
            page.wait_for_function('document.getElementById("cfg-start-date")?.value === "2020-01-01" && _cfgSymbolsLoadSeq > 0')
        except Exception as exc:
            pytest.fail(str(exc) + ' page errors: ' + repr(errors), pytrace=False)
        yield page, control, calls, errors, held
        browser.close()


@pytest.mark.parametrize('mode,expected', [(0, '2024-01-02'), (1, '2024-03-01')])
def test_backtest_uses_optimize_shortcuts(date_page, mode, expected):
    """Real menu, mounted routes and shared buttons set only the base start date."""
    page, control, calls, errors, held = date_page
    before = page.evaluate("JSON.parse(document.getElementById('cfg-raw-json').value)")
    assert page.locator('.cfg-ohlcv-start-btn').nth(mode).get_attribute('title')
    page.locator('.cfg-ohlcv-start-btn').nth(mode).click()
    page.wait_for_function('document.getElementById("cfg-start-date").value === ' + json.dumps(expected))
    page.wait_for_function('JSON.parse(document.getElementById("cfg-raw-json").value).backtest.start_date === ' + json.dumps(expected))
    after = page.evaluate("JSON.parse(document.getElementById('cfg-raw-json').value)")
    assert after['backtest']['end_date'] == before['backtest']['end_date']
    assert after['backtest']['scenarios'] == before['backtest']['scenarios']
    assert any(path == '/api/backtest-v8/ohlcv-start-dates' for _, path in calls)
    page.locator('#pbgui-guide-btn').click()
    page.wait_for_function('document.getElementById("pbgui-shared-help-ovl")?.getAttribute("aria-hidden") === "false"')
    page.locator('#pbgui-shared-help-close').click()
    for viewport, width in [(1280,160),(1280,240),(1280,420),(750,240),(1450,240)]:
        page.set_viewport_size({'width':viewport,'height':900})
        page.evaluate('(width)=>document.getElementById("sidebar").style.width=width+"px"',width)
        assert page.evaluate('Array.from(document.querySelectorAll(".cfg-ohlcv-start-btn")).every(b=>b.getBoundingClientRect().right <= b.closest(".form-group").getBoundingClientRect().right+1)')
    assert not errors


def test_invalid_raw_starts_no_lookup(date_page):
    """Invalid JSON leaves the selected dates and backend state unchanged."""
    page, control, calls, errors, held = date_page
    page.evaluate("document.getElementById('cfg-raw-json').value='{'")
    page.locator('.cfg-ohlcv-start-btn').nth(0).click()
    page.wait_for_function('document.getElementById("cfg-ohlcv-start-status").textContent.includes("Raw JSON")')
    assert not any(path.endswith('/ohlcv-start-dates') for _,path in calls)
    assert page.locator('#cfg-start-date').input_value() == '2020-01-01'
    assert not errors


@pytest.mark.parametrize('detail,expected', [
    ([{'loc': ['body'], 'msg': 'Input should be a valid dictionary', 'input': 'private-fixture'}],
     'Input should be a valid dictionary'),
    ({'message': 'Select at least one exchange', 'input': 'private-fixture'},
     'Select at least one exchange'),
])
def test_lookup_structured_http_error_is_readable(date_page, detail, expected):
    """Real browser fetch renders error messages without stringifying private input."""
    page, control, calls, errors, held = date_page
    control['http_error'] = detail
    page.locator('.cfg-ohlcv-start-btn').nth(0).click()
    page.wait_for_function('document.getElementById("cfg-ohlcv-start-status").textContent.includes(' + json.dumps(expected) + ')')
    message = page.locator('#cfg-ohlcv-start-status').inner_text()
    assert '[object Object]' not in message and 'private-fixture' not in message
    assert page.locator('#cfg-start-date').input_value() == '2020-01-01'
    assert page.locator('.cfg-ohlcv-start-btn').nth(0).is_enabled()
    assert not errors


@pytest.mark.parametrize('date_page',['v7'],indirect=True)
def test_pb7_has_no_pb8_lookup(date_page):
    """PB7 keeps the existing shared calendar without PB8-only shortcuts."""
    assert date_page[0].locator('.cfg-ohlcv-start-btn').count() == 0
