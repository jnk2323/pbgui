"""Selected Backtest rows reach the shared provider-independent AI context."""
import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from browser_driver import sync_browser
from tests.ui.test_backtest_v8_frontend_logic import _extract_function

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('version', ['v7', 'v8'])
def test_marked_results_keep_exact_identity_in_shared_context(version):
    """Click, multiselect and deselect duplicate names without stale or background entities."""
    source = (ROOT / 'frontend/v7_backtest.html').read_text()
    adapter = 'window.PBGUI_AI_PAGE_CONTEXT = function () {' + source.split(
        'window.PBGUI_AI_PAGE_CONTEXT = function () {', 1)[1].split('window.PBGUI_AI_PAGE_ACTIONS', 1)[0]
    rows = [{'path': '/private/' + name, 'result_name': 'Same name', 'backtest_version': version,
             'resource': f'pbgui://backtest/{version}/' + name * 32} for name in ('a', 'b')]
    script = ('window.PBGUI_NAV_CONFIG={current:' + json.dumps(version + '_backtest') + '};'
              'var currentPanel="results",editingConfig="stale-config",selectedArchiveName=null;'
              'var backtestEditorAdapter={version:' + json.dumps(version) + '};'
              'var results=' + json.dumps(rows) + ';'
              'var queueItems=[{filename:"background-job",status:"running"}];'
              'var _selectedResultPaths=new Set();'
              + _extract_function(source, 'getSelectedResults')
              + _extract_function(source, 'setResultRowSelection') + adapter)
    html = ('<nav id="topnav"></nav><div id="results-list"><table class="tbl"><tbody>'
            '<tr id="row-a" data-path="/private/a"><td>Same name</td></tr>'
            '<tr id="row-b" data-path="/private/b"><td>Same name</td></tr>'
            '</tbody></table></div><script>' + script + '</script>'
            '<script src="/app/pbgui_nav.js"></script><script>'
            'document.querySelectorAll("tr").forEach(function(row){row.onclick=function(){'
            'setResultRowSelection(row,!row.classList.contains("selected"));};});</script>')
    with sync_browser() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page()

        def respond(route):
            """Serve only versioned assets and synthetic data, never live runtime state."""
            path = urlparse(route.request.url).path
            if path == '/fixture':
                route.fulfill(body=html, content_type='text/html')
            elif path.startswith('/app/'):
                asset = ROOT / 'frontend' / path.removeprefix('/app/')
                route.fulfill(body=asset.read_bytes() if asset.is_file() else b'',
                              content_type='text/css' if asset.suffix == '.css' else 'text/javascript')
            else:
                route.fulfill(json={})

        page.route('**/*', respond)
        page.goto('http://fixture/fixture')
        assert page.evaluate('PBGuiAI.collectContext().entities') == []
        page.locator('#row-b').click()
        entities = page.evaluate('PBGuiAI.collectContext().entities')
        assert entities == [{'kind': 'backtest_result', 'version': version,
                             'name': 'Same name', 'resource': rows[1]['resource']}]
        assert '/private/' not in json.dumps(entities)
        page.locator('#row-a').click()
        assert {e['resource'] for e in page.evaluate('PBGuiAI.collectContext().entities')} == {r['resource'] for r in rows}
        page.locator('#row-b').click()
        assert page.evaluate('PBGuiAI.collectContext().entities')[0]['resource'] == rows[0]['resource']
        page.locator('#row-a').click()
        assert page.evaluate('PBGuiAI.collectContext().entities') == []
        browser.close()
