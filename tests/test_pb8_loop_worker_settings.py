"""AI experiments cannot tune user-owned worker execution or inherit hardware edits."""
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from pb8_loop_ai_v2 import validate_decision
from pb8_loop_config_contract import (copy_worker_settings, validate_worker_settings,
                                      variant_config_schema, worker_reference)
from pb8_loop_store import validate_change
from tests.test_pb8_loop_optimizer import record, valid_gpu_config


def worker_run(row):
    """Use synthetic automatic worker settings with freely editable search objectives."""
    row['settings'].update(execution='gpu')
    row['initial_config'] = valid_gpu_config()
    row['initial_config']['optimize']['gpu'] = {
        'population_size': None, 'batch_size': None, 'exact_workers': None,
        'auto_lean_parallelism': True, 'validate_per_generation': 8,
        'drift_probes': 4, 'drift_window': 128, 'drift_min_samples': 32,
        'drift_halt': .6}
    return row


@pytest.mark.parametrize('provider', ['chatgpt', 'claude-code', 'opencode-go', 'opencode-zen'])
@pytest.mark.parametrize('edit', ['population', 'batch', 'exact', 'cadence', 'parallelism',
                                 'cpu', 'legacy_population', 'remove_gpu', 'remove_cpu', 'future_gpu'])
def test_every_provider_rejects_worker_edits(record, provider, edit):
    """Native schema and shared backend enforcement reject edits before any job starts."""
    _, row = record
    row = worker_run(row)
    row['settings']['provider'] = provider
    candidate = copy.deepcopy(row['initial_config'])
    opt = candidate['optimize']
    if edit in {'population', 'batch', 'exact', 'cadence', 'parallelism'}:
        key, value = {'population': ('population_size', 512), 'batch': ('batch_size', 1024),
                      'exact': ('exact_workers', 4), 'cadence': ('validate_per_generation', 64),
                      'parallelism': ('auto_lean_parallelism', False)}[edit]
        opt['gpu'][key] = value
    elif edit == 'cpu':
        opt['n_cpus'] = 2
    elif edit == 'legacy_population':
        opt['population_size'] = 512
    elif edit == 'remove_gpu':
        del opt['gpu']
    elif edit == 'remove_cpu':
        del opt['n_cpus']
    else:
        opt['gpu']['future_worker_setting'] = 512
    before = copy.deepcopy(row)
    assert not Draft202012Validator(variant_config_schema(row)).is_valid(candidate)
    with pytest.raises(ValueError, match='fixed Loop configuration contract'):
        validate_decision(row, 'bootstrap', {'max_variants': 1},
                          json.dumps({'variants': [{'config': candidate, 'overrides': {}}]}))
    with pytest.raises(ValueError, match='Worker execution settings are fixed'):
        validate_change(row, candidate, {})
    assert row == before


def test_worker_freeze_keeps_strategy_search_editable(record):
    """The execution boundary does not freeze bot experiments, bounds, scoring or limits."""
    _, row = record
    row = worker_run(row)
    candidate = copy.deepcopy(row['initial_config'])
    candidate['optimize']['scoring'] = [{'metric': 'drawdown_worst_strategy_eq', 'goal': 'min', 'reducer': 'max'}]
    candidate['optimize']['limits'] = [{'metric': 'backtest_completion_ratio', 'penalize_if': 'less_than', 'value': .99}]
    candidate['optimize']['bounds']['long_total_wallet_exposure_limit'] = [.2, .9]
    validate_change(row, candidate, {})
    assert Draft202012Validator(variant_config_schema(row)).is_valid(candidate)


def test_continuation_copy_restores_auto_without_changing_strategy_or_source(record):
    """An old winner's GPU edits cannot become the next run's reviewed defaults."""
    _, row = record
    row = worker_run(row)
    candidate = copy.deepcopy(row['initial_config'])
    candidate['optimize']['gpu'].update(population_size=512, validate_per_generation=64)
    candidate['optimize'].update(n_cpus=32, population_size=512)
    candidate['optimize']['scoring'] = [{'metric': 'mdg_strategy_eq', 'goal': 'max'}]
    before = copy.deepcopy(candidate)
    restored = copy_worker_settings(row['initial_config'], candidate)
    assert restored['optimize']['gpu'] == row['initial_config']['optimize']['gpu']
    assert restored['optimize']['n_cpus'] == row['initial_config']['optimize']['n_cpus']
    assert 'population_size' not in restored['optimize']
    assert restored['optimize']['scoring'] == candidate['optimize']['scoring']
    assert restored['bot'] == candidate['bot'] and restored['backtest'] == candidate['backtest']
    assert candidate == before


def test_existing_run_restoration_uses_trusted_snapshot_without_rewriting_history(record):
    """An explicit restoration can correct a legacy continuation's tainted defaults."""
    _, row = record
    row = worker_run(row)
    approved = copy.deepcopy(row['initial_config'])
    row['initial_config']['optimize']['gpu'].update(population_size=512, validate_per_generation=64)
    row['worker_runtime'] = {'version': 1, 'source_loop': '1' * 32,
                             'settings': {key: copy.deepcopy(approved['optimize'][key]) for key in ('gpu', 'n_cpus')}}
    before = copy.deepcopy(row)
    restored = copy_worker_settings(worker_reference(row), row['initial_config'])
    validate_worker_settings(row, restored)
    assert Draft202012Validator(variant_config_schema(row)).is_valid(restored)
    with pytest.raises(ValueError, match='Worker execution settings are fixed'):
        validate_worker_settings(row, row['initial_config'])
    assert row == before
