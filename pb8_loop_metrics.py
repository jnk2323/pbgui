"""Explicit, provider-independent metric bases for optimizer and CPU evidence."""
from __future__ import annotations

import copy
import math
import statistics

SERVICE = 'PB8LoopMetrics'


def _number(value):
    """Keep missing, boolean and nonfinite metrics distinct from a measured zero."""
    return type(value) in (int, float) and math.isfinite(value)


def _package(record, stats, source, count):
    """Preserve CPU's existing worst-risk/mean-return comparison convention."""
    directions = {key: rule['direction'] for rule in record.get('rubric', []) for key in rule['metrics']}
    reducers = {key: 'max' if directions.get(key) == 'min' else 'mean' for key in stats}
    metrics, missing = {}, {}
    for key, reducer in reducers.items():
        if reducer in stats[key]:
            metrics[key] = stats[key][reducer]
        else:
            missing[key] = reducer
    return {'metrics': metrics, 'metric_statistics': stats,
            'metric_basis': {'version': 1, 'scope': 'training_windows', 'source': source,
                             'window_count': count, 'reducers': reducers, 'unavailable': missing}}


def report_metric_evidence(record, reports):
    """Compute equal-weight statistics from complete finite per-window observations."""
    common = set.intersection(*(set(report) for report in reports)) if reports else set()
    stats = {}
    for key in common:
        values = [report[key] for report in reports]
        if all(_number(value) for value in values):
            # Keep the existing CPU arithmetic exactly, including its precision.
            stats[key] = {'mean': sum(values) / len(values), 'min': min(values), 'max': max(values)}
    return _package(record, stats, 'independent_cpu', len(reports))


def native_metric_evidence(record, data, suite_metrics, labels, summary, config):
    """Read actual native statistics; an aggregate is never assumed to be a maximum."""
    backtest = data.get('backtest') or config.get('backtest') or {}
    suite = bool(suite_metrics or backtest.get('suite_enabled'))
    count = (len(labels) or len(backtest.get('scenarios') or [])) if suite else 1
    stats = {}
    for key in set(summary) | set(suite_metrics):
        block = suite_metrics.get(key)
        values = {}
        if isinstance(block, dict):
            native = block.get('stats') if isinstance(block.get('stats'), dict) else {}
            values.update({name: value for name, value in native.items() if _number(value)})
            scenarios = block.get('scenarios') if isinstance(block.get('scenarios'), dict) else {}
            selected = [scenarios.get(label) for label in labels]
            if labels and all(_number(value) for value in selected):
                for name, value in [('mean', statistics.fmean(selected)), ('min', min(selected)), ('max', max(selected))]:
                    values.setdefault(name, value)
        if not suite and _number(summary.get(key)):
            # A genuine single-window value has identical window extrema.
            values = {'mean': summary[key], 'min': summary[key], 'max': summary[key]}
        # Old suite artifacts with only a configured aggregate have no known
        # reducer. Keep the statistic unavailable rather than infer a bound.
        if values or key in summary:
            stats[key] = values
    return _package(record, stats, 'native_optimizer', count)


def annotate_observations(record, evidence):
    """Label old CPU observations from their retained reports without rewriting history."""
    value = copy.deepcopy(evidence)

    def annotate(observation):
        """Only exact per-report data can reconstruct an unlabelled statistic."""
        if not isinstance(observation, dict):
            return
        candidate = observation.get('candidate')
        if isinstance(candidate, dict) and not candidate.get('metric_basis'):
            candidate['metric_basis'] = {'version': 1, 'source': 'legacy_unlabelled', 'reducers': {}}
        if observation.get('metric_basis'):
            return
        reports = observation.get('per_report')
        if isinstance(reports, list) and reports and all(isinstance(report, dict) for report in reports):
            package = report_metric_evidence(record, reports)
            # Historical flat summaries are retained as recorded; reconstruct
            # labels only when the exact numbers prove the same comparison.
            old = observation.get('metrics') or {}
            package['metric_basis']['reducers'] = {
                key: reducer for key, reducer in package['metric_basis']['reducers'].items()
                if key in old and package['metrics'].get(key) == old[key]}
            observation.update(metric_statistics=package['metric_statistics'], metric_basis=package['metric_basis'])
        else:
            observation['metric_basis'] = {'version': 1, 'source': 'legacy_unlabelled', 'reducers': {}}

    for key in ('baseline', 'best'):
        annotate(value.get(key))
    for observation in value.get('observations') or []:
        annotate(observation)
    for observation in (value.get('winner_options') or {}).values():
        annotate(observation)
    return value
