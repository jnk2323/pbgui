"""Result-owned training and holdout plans survive GUI and AI optimizer reuse."""

import asyncio
import copy
import json

import msgpack
import pytest
from fastapi import HTTPException

from api import optimize_v8
from ai_capabilities import AICapabilityError, AICapabilityService
from scenario_windows import VALIDATION_PLAN_FILENAME, preview_windows


@pytest.fixture
def suite_result(tmp_path, monkeypatch):
    """Create a native result whose private GUI metadata exists only in its sidecar."""
    root = tmp_path / 'results'
    result = root / 'source_run'
    result.mkdir(parents=True)
    windows = [
        {'id': 'one', 'label': 'train_01', 'role': 'training', 'start_date': '2025-01-01', 'end_date': '2025-03-31'},
        {'id': 'two', 'label': 'train_02', 'role': 'training', 'start_date': '2025-04-01', 'end_date': '2025-06-30'},
        {'id': 'three', 'label': 'holdout_01', 'role': 'holdout', 'start_date': '2025-07-01', 'end_date': '2025-09-30'},
    ]
    preview = preview_windows({'start_date': '2025-01-01', 'end_date': '2025-09-30', 'windows': windows})
    config = {'backtest': {'suite_enabled': True, 'exchanges': ['bybit', 'hyperliquid'], 'start_date': '2025-01-01', 'end_date': '2025-09-30',
                          'scenarios': copy.deepcopy(preview['training_scenarios']), 'reducer': {'default': 'mean'}},
              'optimize': {'write_all_results': True}, 'bot': {'long': {'test_parameter': 1}}}
    for row in config['backtest']['scenarios']:
        for key in ('start_date', 'end_date'):
            row[key] += 'T00:00:00'
    (result / VALIDATION_PLAN_FILENAME).write_text(json.dumps(preview['provenance']))
    monkeypatch.setattr(optimize_v8, '_results_root', lambda: root)
    monkeypatch.setattr(optimize_v8, 'prepare_pb8_config', lambda config, **kwargs: copy.deepcopy(config))
    return result, config, preview


@pytest.mark.parametrize('source', ['config.json', 'optimize.json', 'pareto', 'all_results.bin'])
def test_result_editor_recovers_exact_windows_from_each_native_config_source(suite_result, source):
    """Edit and Continue receive training plus holdout without regenerating any dates."""
    result, config, preview = suite_result
    if source == 'pareto':
        (result / 'pareto').mkdir()
        (result / 'pareto/one.json').write_text(json.dumps(config))
    elif source == 'all_results.bin':
        (result / source).write_bytes(msgpack.packb(config))
    else:
        (result / source).write_text(json.dumps(config))
    payload = optimize_v8.get_result_config(str(result), None)['config']
    assert payload['pbgui']['scenario_template'] == preview['provenance']
    assert payload['pbgui']['scenario_generator']['windows'] == preview['parameters']['windows']
    assert payload['backtest']['scenarios'] == preview['training_scenarios']
    assert payload['bot'] == config['bot']
    if source == 'pareto':
        candidate = optimize_v8.get_pareto_file(str(result / 'pareto/one.json'), None)
        assert candidate['config']['pbgui']['scenario_template'] == preview['provenance']
        assert candidate['validation_holdouts'] == preview['holdout_scenarios']


@pytest.mark.parametrize('invalid', ['mismatch', 'disabled', 'malformed', 'symlink', 'oversized', 'nonmidnight'])
def test_invalid_result_plan_never_disappears_silently(suite_result, invalid):
    """A corrupt or unrelated sidecar blocks reuse instead of silently losing the holdout."""
    result, config, _ = suite_result
    path = result / VALIDATION_PLAN_FILENAME
    if invalid == 'mismatch':
        config['backtest']['scenarios'][0]['end_date'] = '2025-03-30'
    elif invalid == 'disabled':
        config['backtest']['suite_enabled'] = False
    elif invalid == 'malformed':
        path.write_text('not json')
    elif invalid == 'symlink':
        target = result.parent / 'unrelated.json'
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif invalid == 'oversized':
        path.write_text(' ' * (256 * 1024 + 1))
    else:
        config['backtest']['scenarios'][0]['start_date'] = '2025-01-01T12:00:00'
    (result / 'config.json').write_text(json.dumps(config))
    with pytest.raises(HTTPException) as error:
        optimize_v8.get_result_config(str(result), None)
    assert error.value.status_code == 422
    assert 'Training/Holdout' in error.value.detail


def test_legacy_result_without_validation_metadata_is_unchanged(suite_result):
    """No holdout is guessed when an old run contains only training scenarios."""
    result, config, _ = suite_result
    (result / VALIDATION_PLAN_FILENAME).unlink()
    assert optimize_v8._restore_result_window_plan(result, config) is config


@pytest.mark.parametrize('broken', [False, True])
def test_checkpoint_resume_preserves_windows_before_any_save(suite_result, tmp_path, monkeypatch, broken):
    """Native checkpoint handoff keeps provenance or fails before creating a config/job."""
    result, config, preview = suite_result
    saved = []
    monkeypatch.setattr(optimize_v8, '_configs_dir', lambda: tmp_path / 'configs')
    monkeypatch.setattr(optimize_v8, '_checkpoint_resume_readiness', lambda directory: {'ready': True, 'config': config})
    monkeypatch.setattr(optimize_v8, '_preflight_checkpoint_resume', lambda *args: None)
    monkeypatch.setattr(optimize_v8, '_save_config_bundle', lambda name, candidate, **kwargs: saved.append(candidate) or candidate)
    monkeypatch.setattr(optimize_v8, '_load_override_payloads', lambda *args: {})
    monkeypatch.setattr(optimize_v8, '_create_queue_record', lambda name, prepared, *args: {'config': prepared})
    if broken:
        (result / VALIDATION_PLAN_FILENAME).write_text('invalid')
        with pytest.raises(HTTPException):
            optimize_v8.queue_result_resume({'path': str(result), 'name': 'resumed_suite'}, None)
        assert saved == []
    else:
        response = optimize_v8.queue_result_resume({'path': str(result), 'name': 'resumed_suite'}, None)
        assert saved[0]['pbgui']['scenario_template'] == preview['provenance']
        assert response['config']['backtest']['scenarios'] == preview['training_scenarios']


@pytest.mark.parametrize('existing', [False, True])
def test_ai_suite_reuse_preserves_selected_bot_and_native_windows(suite_result, tmp_path, monkeypatch, existing):
    """One immutable proposal combines selected strategy with explicitly sourced training windows."""
    import ai_capabilities
    result, config, preview = suite_result
    (result / 'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(ai_capabilities, 'load_ini_section', lambda section: {})
    current = {'pbgui': {'note': 'retain', 'scenario_generator': {'windows': []}}} if existing else None

    async def exercise():
        """Use the common provider proposal path without saving or starting anything."""
        service = AICapabilityService(tmp_path / 'capabilities')
        resource = service._virtual_uri('optimizer-run', 'v8', str(result))
        monkeypatch.setattr(service, '_resolve_listed_resource', lambda kind, version, uri: (
            {'path': str(result)} if (kind, version, uri) == ('optimizer-run', 'v8', resource) else pytest.fail('wrong source')))
        monkeypatch.setattr(service, '_current_pb8_bundle', lambda name: (copy.deepcopy(current), {}, 'old' if existing else None))
        monkeypatch.setattr(service, '_validate_pb8_config', lambda name, config: copy.deepcopy(config))
        monkeypatch.setattr(service, '_queue_preview', lambda config, overrides: {})
        selected = {'backtest': {'suite_enabled': False, 'exchanges': ['bybit'], 'start_date': '2025-07-01', 'end_date': '2025-09-30'},
                    'bot': {'long': {'test_parameter': 99}}, 'optimize': {'iters': 20_000_000}}
        args = {'name': 'reuse_suite', 'config': selected, 'suite_source_run_resource': resource, 'action': 'save'}
        response = await service._propose_pb8_optimizer_config('a' * 32, 'b' * 32, args)
        proposal = service.proposals[response['proposal_id']]
        assert proposal.config['bot'] == selected['bot']
        assert proposal.config['optimize']['iters'] == 20_000_000
        assert proposal.config['backtest']['start_date'] == '2025-01-01'
        assert proposal.config['backtest']['exchanges'] == ['bybit', 'hyperliquid']
        assert proposal.config['backtest']['scenarios'] == preview['training_scenarios']
        assert proposal.config['pbgui']['scenario_template'] == preview['provenance']
        assert 'scenario_generator' not in proposal.config['pbgui']
        assert proposal.status == 'awaiting_approval'
        assert selected['backtest']['suite_enabled'] is False
        await service.list_proposals('a' * 32, 'b' * 32)
        assert proposal.config['pbgui']['scenario_template'] == preview['provenance']
        assert 'scenario_generator' not in proposal.config['pbgui']
        with pytest.raises(AICapabilityError, match='either'):
            await service._propose_pb8_optimizer_config('a' * 32, 'c' * 32, dict(args, scenario_template={}))
        (result / VALIDATION_PLAN_FILENAME).unlink()
        with pytest.raises(AICapabilityError, match='no complete saved'):
            await service._propose_pb8_optimizer_config('a' * 32, 'd' * 32, args)
        await service.shutdown()

    asyncio.run(exercise())
