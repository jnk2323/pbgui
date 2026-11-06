"""Bounded initial context with exact current evidence and archive references."""
import copy
import json

import pytest

from pb8_loop_evidence import EvidenceStore, EvidenceSession, required_content, select_findings, encode, tokens
from pb8_loop_ai_policy import check_context
from tests.test_pb8_loop_optimizer import record, RULE


def test_one_hundred_rounds_do_not_reinject_full_historical_reports(record):
    """The findings budget stays fixed while immutable optional provenance grows."""
    store,row = record
    evidence = EvidenceStore(store)
    entries = []
    lengths = []
    for index in range(100):
        entries.append({'id':'round:'+str(index),'loop':row['id'],'created_at':index,
            'status':'observed_exact','coins':row['settings']['goals']['coins'],
            'fingerprint':row['fingerprint'],'knowledge':'Measured hypothesis '+str(index),
            'observations':[{'exact':True,'metrics':{'unused_'+str(key):key for key in range(240)}}]})
        package, originals = select_findings(entries,row,1000)
        content = {'stage':'evaluate','initial_config':{},'rubric':RULE,'knowledge':package,
                   'evidence':{'observations':[{'metrics':{'gain':2,'liquidated':0}}]}}
        required = required_content(content)
        lengths.append(tokens(required))
        assert 'observations' not in encode(package) and tokens(package['findings']) <= 1000
    assert max(lengths) - min(lengths) < 1200
    frozen = evidence.create(row,required,content,originals)
    assert len(frozen['history']) == 100
    assert evidence.resolve(frozen,'/history/round:0/observations/0/metrics/unused_239') == 239


@pytest.mark.parametrize('count',[100,512,1000])
def test_selection_columns_preserve_every_id_and_current_safety_value(count):
    """There is no reader-prefix or arbitrary shortlist hidden in the context projector."""
    candidates = [{'id':'candidate_'+str(index),'optimizer_job':'owned',
                   'config':{'distinct':index},'metrics':{'gain':index+.1234567890123456,'liquidated':0,
                   'drawdown_worst_strategy_eq':.49,'unrelated_extra':index}} for index in range(count)]
    content = {'stage':'select','rubric':RULE,'initial_config':{},'evidence':{'candidates':candidates}}
    before = copy.deepcopy(content)
    required = required_content(content)
    assert len(required['evidence']['candidates']) == count
    table = required['evidence']['candidates_metrics']
    assert len(table['rows']) == count and 'unrelated_extra' not in table['columns']
    for index,row in enumerate(table['rows']):
        assert row[table['columns'].index('gain')] == candidates[index]['metrics']['gain']
        assert row[table['columns'].index('liquidated')] == 0
    assert content == before


def test_page_measurement_includes_escaped_wrapper_and_scalar_fragments(record):
    """Nested quotes and Unicode are budgeted using the exact delivered response shape."""
    store,row = record
    required = {'text':'"\\\nα🙂' * 5000,'safety':{'liquidated':None}}
    archive = EvidenceStore(store)
    frozen = archive.create(row,required,{'stage':'select'},[])
    session = EvidenceSession(archive,frozen,1500)
    assert all(tokens(page) <= 1500 for page in session.required_pages)
    fragments = [entry for page in session.required_pages for entry in page['entries'] if entry['path']=='/text']
    assert ''.join(entry['value'] for entry in fragments) == required['text']


def test_large_selection_is_bounded_deterministic_and_dispatches(record):
    """Large native runs yield a normal decision with conservative 256k context."""
    from pb8_loop_dialog import OpenCodeDialog
    import asyncio
    store, row = record
    candidates = [{'id': f'candidate_{i:05d}', 'optimizer_job': 'owned',
                   'source': {'coverage': 'exact provenance ' * 100},
                   'metrics': {'gain': i, 'liquidated': 0, 'drawdown_worst_strategy_eq': .49}}
                  for i in range(1272)]
    content = {'stage': 'select', 'rubric': RULE, 'initial_config': {},
               'evidence': {'candidates': candidates}}
    before = copy.deepcopy(content)
    required = required_content(content, selection_token_budget=20000)
    assert required == required_content(content, selection_token_budget=20000)
    assert content == before
    summary = required['evidence']['candidate_selection']
    assert 0 < summary['included'] < summary['total'] == 1272
    assert summary['included'] + summary['trimmed'] == 1272
    assert tokens(required) <= 20000
    assert 'candidate_01271' in encode(required)  # gain leader, never a prefix
    table = required['evidence']['candidates_metrics']
    for candidate in required['evidence']['candidates']:
        assert table['rows'][candidate['metrics_row']][table['columns'].index('liquidated')] == 0
    archive = EvidenceStore(store)
    snapshot = archive.create(row, required, content, [])
    assert len(archive.resolve(snapshot, summary['originals_path'])) == 1272
    for candidate in required['evidence']['candidates']:
        assert archive.resolve(snapshot, candidate['source']['evidence_path']) == candidates[0]['source']
    profile = {'context': 256000, 'page_tokens': 5000, 'output_tokens': 32768,
               'tool_mode': 'native', 'streaming': False, 'call_timeout': 10}
    from pb8_loop_ai_v2 import validate_snapshot_budget
    legacy_snapshot = {**snapshot, 'required': required_content(content)}
    validate_snapshot_budget(row, legacy_snapshot, {'protocol': 'chat', 'loop_profile': profile})
    async def send(body, runtime, remaining):
        check_context(body, runtime, 32768, initial=True)
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"candidate_ids":["candidate_01271"]}'}}], 'usage': {'input_tokens': 100, 'output_tokens': 10}}
    class Ledger:
        def remaining(self): return 10
        def reserve(self, *args, **kwargs): return 'call'
        def settle(self, *args, **kwargs): pass
    session = EvidenceSession(archive, snapshot)
    dialog = OpenCodeDialog(row['settings'], {'protocol': 'chat'}, profile, session, Ledger(), send)
    text = asyncio.run(dialog.run('Select exact candidates.', required, 32768))
    assert json.loads(text)['candidate_ids'] == ['candidate_01271']
