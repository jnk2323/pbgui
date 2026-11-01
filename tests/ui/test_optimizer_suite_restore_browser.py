"""Recovered native window metadata renders and roundtrips in the real Suite editor."""

import json
from pathlib import Path

from api import optimize_v8
from test_optimizer_suite_restore import suite_result

ROOT = Path(__file__).resolve().parents[2]


def test_recovered_training_and_holdout_are_visible_and_survive_save(suite_result):
    """No Generate or Apply action is necessary to view an imported run's exact plan."""
    result, config, preview = suite_result
    (result / 'config.json').write_text(json.dumps(config))
    restored = optimize_v8.get_result_config(str(result), None)['config']
    browser_api = __import__('browser_driver')
    with browser_api.sync_browser() as runner:
        browser = runner.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1400, 'height': 1100})
            page.route('**/*', lambda route: route.fulfill(json={}))
            page.set_content('<div id="suite"></div>')
            page.add_script_tag(path=str(ROOT / 'frontend/js/scenario_visual_editor.js'))
            page.add_script_tag(path=str(ROOT / 'frontend/js/suite_editor.js'))
            page.evaluate("""cfg => {
                window.esc = s => String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
                window.toast=()=>{}; window.scheduleStructuredEditorSync=()=>{};
                window.apiFetch=()=>Promise.resolve({params:[]});
                suiteInit('suite',{version:'v8',scenarioGenerator:true,getScenarioContext:()=>({
                    start_date:cfg.backtest.start_date,end_date:cfg.backtest.end_date,exchanges:[]})});
                suiteLoad(cfg);
            }""", restored)
            assert page.locator('.scenario-canvas [data-part=move]').count() == 3
            assert page.get_by_role('button', name='holdout_01 2025-07-01 to 2025-09-30', exact=True).is_visible()
            collected = page.evaluate('suiteCollect()')
            assert collected['suite_enabled'] is True
            assert collected['scenarios'] == preview['training_scenarios']
            assert collected['scenario_template']['holdout_scenarios'] == preview['holdout_scenarios']
            page.evaluate("""suite => suiteLoad({backtest:{suite_enabled:suite.suite_enabled,
                scenarios:suite.scenarios,reducer:suite.aggregate},pbgui:{
                scenario_template:suite.scenario_template,scenario_generator:suite.scenario_generator}})""", collected)
            assert page.locator('.scenario-canvas [data-part=move]').count() == 3
            assert page.evaluate('_suiteVisualWindows().filter(w=>w.role==="holdout")[0].start_date') == '2025-07-01'
        finally:
            browser.close()
