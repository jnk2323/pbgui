"""Approval-gated PB8 result reruns preserve the source and exact queue identity."""
import asyncio
import copy

import pytest

from ai_capabilities import AICapabilityError, AICapabilityService

OWNER = 'a' * 32
CONVERSATION = 'b' * 32
RESOURCE = 'pbgui://backtest/v8/' + 'c' * 32


@pytest.fixture
def rerun(tmp_path, monkeypatch):
    """Keep every filesystem operation and queue effect inside synthetic fixtures."""
    from api import backtest_v8
    service = AICapabilityService(tmp_path / 'capabilities')
    result_dir = tmp_path / 'source'
    result_dir.mkdir()
    source = {
        'backtest': {'start_date': '2026-06-17', 'end_date': '2026-10-28',
                     'exchanges': ['bybit', 'hyperliquid'], 'suite_enabled': True,
                     'scenarios': [{'label': 'holdout', 'start_date': '2026-06-17'}],
                     'starting_balance': 100000, 'reducer': {'default': 'mean'}},
        'bot': {'long': {'strategy': {'ema_anchor': {'base_qty_pct': 0.0768}},
                         'risk': {'n_positions': 1, 'total_wallet_exposure_limit': 7.5}},
                'short': {'risk': {'n_positions': 0}}},
        'live': {'approved_coins': {'long': ['HYPE'], 'short': []}},
        'pbgui': {'scenario_template': {'template': 'ai_loop_windows'}},
    }
    overrides = {'HYPE.json': {'bot': {'long': {'strategy': {'ema_anchor': {'offset': 0.02}}}}}}
    prepared, queued = [], []

    def resolve(kind, version, resource):
        """Resolve only the exact managed reference, never an arbitrary path."""
        assert (kind, version, resource) == ('backtest', 'v8', RESOURCE)
        return {'path': str(result_dir), 'config_name': 'original_loop_result'}

    def resolve_dir(path, allow_legacy):
        """Retain the managed-root and legacy restriction in the fixture boundary."""
        assert path == str(result_dir) and allow_legacy is False
        return result_dir

    def prepare(config, **kwargs):
        """Record native validation separately from any queue submission."""
        prepared.append(copy.deepcopy(config))
        return config

    def queue(body, session):
        """Model the native queue's operation-ID deduplication across retries."""
        for item in queued:
            if item['operation_id'] == body['operation_id']:
                return {'filename': item['operation_id']}
        queued.append(copy.deepcopy(body))
        return {'filename': body['operation_id']}

    monkeypatch.setattr(service, '_resolve_listed_resource', resolve)
    monkeypatch.setattr(backtest_v8, '_resolve_result_dir', resolve_dir)
    monkeypatch.setattr(backtest_v8, 'get_result_config', lambda *args, **kwargs: source)
    monkeypatch.setattr(backtest_v8, '_load_override_payloads', lambda *args: copy.deepcopy(overrides))
    monkeypatch.setattr(backtest_v8, '_validate_override_payloads', lambda *args: copy.deepcopy(overrides))
    monkeypatch.setattr(backtest_v8, '_normalize_config', lambda config, name: config)
    monkeypatch.setattr(backtest_v8, '_config_file', lambda name: tmp_path / 'configs' / name / 'config.json')
    monkeypatch.setattr(backtest_v8, 'prepare_pb8_config', prepare)
    monkeypatch.setattr(backtest_v8, 'get_settings', lambda session: {
        'exchange_options': ['bybit', 'hyperliquid'], 'autostart': True})
    monkeypatch.setattr(backtest_v8, 'add_to_queue', queue)
    return service, source, overrides, prepared, queued


def request(**changes):
    """Build one complete explicit full-range request."""
    return {'version': 'v8', 'resource': RESOURCE, 'start_date': '2024-12-05',
            'end_date': '2026-10-30', 'exchanges': ['bybit', 'hyperliquid'], **changes}


def test_rerun_uses_one_approval_and_two_immutable_idempotent_jobs(rerun):
    """Preparation cannot queue; approval snapshots dates/exchanges without changing bot settings."""
    service, source, overrides, prepared, queued = rerun
    original = copy.deepcopy(source)

    async def scenario():
        """Exercise dispatcher, durable review, owner checks and the existing queue journal."""
        try:
            created = await service.dispatch(OWNER, CONVERSATION, 'propose_backtest_from_result', request())
            assert not queued and len(prepared) == 1
            assert created['status'] == 'awaiting_approval'
            preview = created['preview']
            assert preview['job_count'] == 2 and preview['may_start_immediately'] is True
            assert (preview['start_date'], preview['end_date']) == ('2024-12-05', '2026-10-30')
            assert '/source' not in str(preview)
            pending = service.proposals[created['proposal_id']]
            for owner, conversation, digest in [('d' * 32, CONVERSATION, pending.payload_digest),
                                                 (OWNER, 'e' * 32, pending.payload_digest),
                                                 (OWNER, CONVERSATION, 'wrong')]:
                with pytest.raises(AICapabilityError):
                    await service.approve(owner, pending.id, digest, conversation)
            assert not queued
            # Subsequent source changes cannot silently alter an already reviewed snapshot.
            source['bot']['long']['risk']['total_wallet_exposure_limit'] = 1
            result = await service.approve(OWNER, pending.id, pending.payload_digest, CONVERSATION)
            replay = await service.approve(OWNER, pending.id, pending.payload_digest, CONVERSATION)
            assert result == replay and result['queued_count'] == 2
            assert result['compare_after_completion'] is True and len(queued) == 2
            assert queued[0]['operation_id'] != queued[1]['operation_id']
            for job, exchange in zip(queued, ['bybit', 'hyperliquid']):
                config = job['config']
                assert config['bot'] == original['bot'] and config['live'] == original['live']
                assert config['backtest']['starting_balance'] == 100000
                assert config['backtest']['exchanges'] == [exchange]
                assert config['backtest']['start_date'] == '2024-12-05'
                assert config['backtest']['end_date'] == '2026-10-30'
                assert not {'suite_enabled', 'scenarios', 'reducer'} & config['backtest'].keys()
                assert 'scenario_template' not in config['pbgui']
                assert job['override_configs'] == overrides
        finally:
            await service.shutdown()

    asyncio.run(scenario())
    # Only the deliberate fixture mutation above occurred; no dates/coins/source config changed.
    original['bot']['long']['risk']['total_wallet_exposure_limit'] = 1
    assert source == original


@pytest.mark.parametrize('changes', [
    {'version': 'v7'}, {'resource': '../escape'}, {'resource': 'pbgui://backtest/v8/' + 'd' * 31},
    {'exchanges': []}, {'exchanges': ['bybit', 'bybit']}, {'exchanges': ['unsupported']},
    {'exchanges': [None]}, {'exchanges': ['bybit'] * 6},
    {'start_date': '2026-02-30'}, {'start_date': '2026-11-01'},
    {'end_date': 'now'}, {'config': {'bot': {}}},
])
def test_invalid_rerun_never_creates_proposal_or_job(rerun, changes):
    """Reject invalid identity, dates, exchanges and attempted strategy injection before approval."""
    service, source, _, _, queued = rerun
    original = copy.deepcopy(source)
    with pytest.raises(AICapabilityError):
        asyncio.run(service.dispatch(OWNER, CONVERSATION, 'propose_backtest_from_result', request(**changes)))
    assert not service.proposals and not queued and source == original


def test_rerun_is_not_available_to_analysis_only_conversation(rerun):
    """Reading untrusted research can never acquire a new execution capability."""
    service, _, _, _, queued = rerun
    service.restrict_to_analysis(OWNER, CONVERSATION)
    with pytest.raises(AICapabilityError):
        asyncio.run(service.dispatch(OWNER, CONVERSATION, 'propose_backtest_from_result', request()))
    assert not service.proposals and not queued


@pytest.mark.parametrize('boundary', ['source', 'overrides', 'native'])
def test_unavailable_or_invalid_source_never_creates_review(rerun, monkeypatch, boundary):
    """Missing managed evidence, overrides or native validity cannot be deferred past approval."""
    from api import backtest_v8
    from fastapi import HTTPException
    from pb8_config import PB8ConfigurationError
    service, _, _, _, queued = rerun

    def reject(*args, **kwargs):
        """Simulate the actual guarded API/native error at one preparation boundary."""
        if boundary == 'native':
            raise PB8ConfigurationError('Fixture invalid config')
        raise HTTPException(status_code=404, detail='Fixture source unavailable')

    monkeypatch.setattr(backtest_v8, {'source': 'get_result_config', 'overrides': '_load_override_payloads',
                                    'native': 'prepare_pb8_config'}[boundary], reject)
    with pytest.raises(AICapabilityError):
        asyncio.run(service.dispatch(OWNER, CONVERSATION, 'propose_backtest_from_result', request()))
    assert not service.proposals and not queued


def test_all_provider_formats_offer_the_same_reviewed_rerun_tool(rerun):
    """Codex, Responses, Messages/Claude and Chat Completions share the capability."""
    service = rerun[0]
    name = 'propose_backtest_from_result'
    assert name in {tool['name'] for namespace in service.codex_dynamic_tools() for tool in namespace['tools']}
    assert name in {tool['name'] for tool in service.responses_tools()}
    assert name in {tool['name'] for tool in service.messages_tools()}
    assert name in {tool['function']['name'] for tool in service.chat_completion_tools()}
