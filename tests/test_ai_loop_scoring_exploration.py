"""Template searches expose the full scoring catalog without forcing per-round choices."""
import copy
import json

import pytest

from ai_loop_objectives import scoring_objectives, validate_objective, validate_template_rubric
from ai_loop_request import bind_request
from pb8_loop_exploration import exploration_policy, validate_exploration
from tests.test_ai_loop_objectives import reviewed_run, RUBRIC
from tests.test_ai_loop_windows import request
from tests.test_pb8_loop_optimizer import record
from vast_config_validation import METRICS, REDUCERS


def search_run(tmp_path, scores=None):
    """Represent completed synthetic searches only in temporary storage."""
    _, row, _ = reviewed_run(tmp_path)
    row['rubric'] = copy.deepcopy(RUBRIC)
    row['initial_config']['optimize']['limits'] = [
        {'metric': 'drawdown_worst_strategy_eq', 'penalize_if': 'greater_than', 'reducer': 'max', 'value': .5}]
    row['initial_config']['optimize']['bounds'] = {'long': {'risk': {'first': [0, 10], 'second': [0, 10]}}}
    row['jobs'] = []
    for number, score in enumerate(scores or [[{'metric': 'adg_strategy_eq', 'goal': 'max', 'reducer': 'mean'}]]):
        config = copy.deepcopy(row['initial_config'])
        config['optimize']['scoring'] = score
        row['jobs'].append({'kind': 'optimizer', 'operation': str(number + 1) * 32,
                            'round': number, 'config': config, 'started': True,
                            'status': 'cancelled', 'results_consumed': True, 'result_count': 1})
    return row


def test_cpu_gain_is_not_a_fixed_gpu_scoring_formula(tmp_path):
    """Gain stays the exact winner criterion while many supported proxies can guide search."""
    objective = {'metric': 'gain_strategy_eq', 'goal': 'max', 'reducer': 'mean'}
    assert validate_objective(objective) == objective
    _, row, _ = reviewed_run(tmp_path)
    original = copy.deepcopy(row['initial_config'])
    for metric in METRICS:
        for reducer in REDUCERS:
            candidate = copy.deepcopy(original)
            candidate['optimize']['scoring'] = [{'metric': metric, 'goal': 'max', 'reducer': reducer}]
            assert scoring_objectives(candidate)
            validate_template_rubric(row, candidate, RUBRIC)
    assert row['initial_config'] == original
    validate_template_rubric(row, original, RUBRIC)


@pytest.mark.parametrize('experiment', ['bounds', 'seed', 'repeat', 'combination'])
def test_ai_can_choose_controlled_experiments_without_forced_switches(tmp_path, experiment):
    """Controlled comparisons and repeated seeds remain possible after stagnation."""
    row = search_run(tmp_path)
    candidate = copy.deepcopy(row['jobs'][-1]['config'])
    if experiment == 'bounds':
        candidate['optimize']['bounds']['long']['risk']['first'][1] = 10.5
    elif experiment == 'seed':
        candidate['optimize']['seed'] = 42
    elif experiment == 'combination':
        candidate['optimize']['scoring'].append({'metric': 'drawdown_worst_strategy_eq', 'goal': 'min', 'reducer': 'max'})
    validate_exploration(row, candidate, stagnation_after=5)
    validate_template_rubric(row, candidate, RUBRIC)


def test_freedom_does_not_remove_hard_user_filters(tmp_path):
    """Adaptive experiments cannot loosen the user drawdown ceiling."""
    row = search_run(tmp_path)
    candidate = copy.deepcopy(row['initial_config'])
    candidate['optimize']['limits'][0]['value'] = .6
    with pytest.raises(ValueError, match='hard user-goal filter'):
        validate_exploration(row, candidate)


def test_catalog_is_complete_and_unstarted_attempt_is_not_tested(tmp_path):
    """Coverage is based on actual consumed optimizer output, not proposed configurations."""
    row = search_run(tmp_path)
    pending = copy.deepcopy(row['jobs'][0])
    pending.update(round=0, operation='f' * 32, started=False, results_consumed=False, result_count=0)
    row['history'] = [{'round': 0, 'observations': [{'exact': True, 'metrics': {'gain_strategy_eq': 2}}]}]
    pending['config']['optimize']['scoring'].append({'metric': 'drawdown_worst_strategy_eq', 'goal': 'min', 'reducer': 'max'})
    row['jobs'].append(pending)
    coverage = exploration_policy(row)['scoring_exploration']
    assert set(coverage['catalog']['metrics']) == set(METRICS)
    assert set(coverage['catalog']['reducers']) == set(REDUCERS)
    assert coverage['tested_metrics'] == ['adg_strategy_eq']
    assert 'drawdown_worst_strategy_eq' in coverage['untested_metrics']
    assert len(coverage['tested_scoring_signatures']) == 1


def test_coverage_informs_without_prescribing_next_experiment(tmp_path):
    """Catalog coverage is advice, not a per-round metric or filter quota."""
    row = search_run(tmp_path, [[{'metric': 'adg_strategy_eq', 'goal': 'max', 'reducer': reducer}]
                               for reducer in ['mean', 'min']])
    policy = exploration_policy(row)
    assert policy['mode'] == 'adaptive'
    coverage = policy['scoring_exploration']
    assert not coverage['required']
    assert not coverage['require_untested_metric']
    assert not coverage['require_new_optional_limits']


@pytest.mark.parametrize('execution', ['gpu', 'vast'])
@pytest.mark.parametrize('experiment', ['bounds', 'seed', 'repeat', 'population'])
def test_legacy_continuation_requires_material_search_change(tmp_path, execution, experiment):
    """A continuation cannot silently weaken broad exploration into small edits."""
    row = search_run(tmp_path)
    row['settings'].update(execution=execution, source={})
    row['continuation'] = {'parent': '1' * 32, 'additional_runs': 25}
    row.update(round=10, stagnation=8)
    candidate = copy.deepcopy(row['jobs'][-1]['config'])
    if experiment == 'bounds':
        candidate['optimize']['bounds']['long']['risk']['first'][1] = 10.5
    elif experiment == 'seed':
        candidate['optimize']['seed'] = 42
    elif experiment == 'population':
        candidate['optimize'].setdefault('gpu', {})['population_size'] = 1024
    before = copy.deepcopy(row)
    assert exploration_policy(row)['mode'] == 'broad'
    with pytest.raises(ValueError, match='Broad exploration requires'):
        validate_exploration(row, candidate, stagnation_after=9)
    candidate['optimize']['limits'][0]['value'] = .6
    with pytest.raises(ValueError, match='hard user-goal filter'):
        validate_exploration(row, candidate)
    assert row == before


@pytest.mark.parametrize('provider', ['chatgpt', 'claude-code', 'opencode-go', 'opencode-zen'])
def test_legacy_gpu_provider_gate_requires_material_change_and_protects_dates(record, provider):
    """All adapters reject tiny follow-ups while accepting a different objective set."""
    from pb8_loop_ai_v2 import validate_decision
    from tests.test_pb8_loop_optimizer import valid_gpu_config
    _, row = record
    row['settings'].update(provider=provider, execution='gpu', source={})
    row['initial_config'] = valid_gpu_config()
    row['jobs'] = [{'kind': 'optimizer', 'operation': 'previous', 'round': 10,
                    'config': copy.deepcopy(row['initial_config'])}]
    evidence = {'winner_options': {'winner': {}}, 'remaining_rounds': 14,
                'remaining_runs': 14, 'free_slots': 1, 'exploration_policy': exploration_policy(row)}
    decision = {'winner_operation': 'winner', 'reason': 'Retain the independently qualified incumbent',
                'finish': False, 'variants': [{'config': copy.deepcopy(row['initial_config']),
                'overrides': {}, 'reason': 'Repeat the promising scoring with a fresh population to test repeatability'}]}
    with pytest.raises(ValueError, match='Broad exploration requires'):
        validate_decision(row, 'evaluate', evidence, json.dumps(decision))
    decision['variants'][0]['config']['optimize']['scoring'].append(
        {'metric': 'drawdown_worst_strategy_eq', 'goal': 'min', 'reducer': 'max'})
    decision['variants'][0]['reason'] = 'Test growth against worst-window risk on a new Pareto front'
    assert validate_decision(row, 'evaluate', evidence, json.dumps(decision))['variants']
    decision['variants'][0]['reason'] = ''
    with pytest.raises(ValueError, match='hypothesis'):
        validate_decision(row, 'evaluate', evidence, json.dumps(decision))
    decision['variants'][0]['reason'] = 'Keep the same training window'
    decision['variants'][0]['config']['backtest']['end_date'] = '2030-01-01'
    with pytest.raises(ValueError, match='fixed Loop configuration contract'):
        validate_decision(row, 'evaluate', evidence, json.dumps(decision))
