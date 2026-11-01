"""Isolated report rendering, ownership checks and API-owned process lifecycle."""
import asyncio
import copy
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
import json
from types import SimpleNamespace

import pytest

from pb8_loop_report_worker import LoopReportWorker, LoopReportUnavailable, LoopSnapshotMissing, build_report
from tests.test_pb8_loop_optimizer import OWNER, record


def test_spawned_reports_match_owned_projection_and_join_children(record):
    """Real spawned readers use only the temporary owned archive and close deterministically."""
    from api import loop_optimizer_v8 as api
    from pb8_loop_storage import LoopArchive, LoopStorageError
    store, row = record
    expected = api.projection(store.read(OWNER, row['id']))
    worker = LoopReportWorker()
    processes = list(worker._pool._processes.values())
    try:
        created, content = worker.build(store.root, OWNER, row['id'])
        actual = json.loads(content)
        actual.pop('view_revision')
        assert actual == json.loads(json.dumps(expected))
        assert created == row['created_at']
        with pytest.raises(FileNotFoundError):
            worker.build(store.root, 'f' * 32, row['id'])
        store.update(OWNER, row['id'], lambda value: value.update(status='paused', reason='New state'))
        assert json.loads(worker.build(store.root, OWNER, row['id'])[1])['status'] == 'paused'
        config = copy.deepcopy(row['initial_config'])
        config['optimize']['iters'] = 777
        snapshot = {'operation': 'a' * 32, 'kind': 'optimizer', 'config': config, 'overrides': {}}
        store.update(OWNER, row['id'], lambda value: value.update(jobs=[snapshot]))
        diff = worker.diff(store.root, OWNER, row['id'], snapshot['operation'], 'initial')
        assert {'path': 'optimize.iters', 'before': row['initial_config']['optimize'].get('iters'),
                'after': 777} in diff['fields']
        with pytest.raises(LoopSnapshotMissing):
            worker.diff(store.root, OWNER, row['id'], 'b' * 32, 'initial')
        with pytest.raises(FileNotFoundError):
            worker.diff(store.root, 'f' * 32, row['id'], snapshot['operation'], 'initial')
        path = store.path(OWNER, row['id'])
        before = path.read_bytes()
        root = json.loads(before)['_loop_storage']['root'][1]
        with LoopArchive(path.parent, row['id']).connection(write=True) as session:
            session.connection.execute('UPDATE objects SET payload = ? WHERE hash = ?', (b'corrupt', root))
        with pytest.raises(LoopStorageError):
            worker.build(store.root, OWNER, row['id'])
        assert path.read_bytes() == before
    finally:
        worker.close()
        worker.close()
    assert processes and all(not process.is_alive() for process in processes)
    with pytest.raises(LoopReportUnavailable):
        worker.build(store.root, OWNER, row['id'])


def test_broken_pool_is_retired_once_and_replaced(monkeypatch):
    """A worker failure cannot permanently poison future automatic report retries."""
    import pb8_loop_report_worker as module
    pools = []

    class Pool:
        """Resolve warm-ups while simulating one crashed report executor."""
        def __init__(self, **kwargs):
            self.shutdowns = []
            pools.append(self)
            assert kwargs['mp_context'].get_start_method() == 'spawn'

        def submit(self, function, *_args):
            future = Future()
            if function is build_report and self is pools[0]:
                future.set_exception(BrokenProcessPool('Fixture process stopped'))
            else:
                future.set_result((1, b'{}') if function is build_report else None)
            return future

        def shutdown(self, **kwargs):
            self.shutdowns.append(kwargs)

    monkeypatch.setattr(module, 'ProcessPoolExecutor', Pool)
    worker = LoopReportWorker()
    for _ in range(8):
        assert worker._slots.acquire(blocking=False)
    with pytest.raises(LoopReportUnavailable, match='busy'):
        worker.build('temporary', OWNER, 'a' * 32)
    assert len(pools) == 1
    for _ in range(8):
        worker._slots.release()
    assert worker.build('temporary', OWNER, 'a' * 32) == (1, b'{}')
    assert len(pools) == 2 and pools[0].shutdowns == [{'wait': True, 'cancel_futures': True}]
    worker.close()
    worker.close()
    assert pools[1].shutdowns == [{'wait': True, 'cancel_futures': True}]


@pytest.mark.parametrize('controller_failure', [False, True])
def test_api_shutdown_joins_report_workers_even_if_controller_fails(monkeypatch, controller_failure):
    """Native jobs remain independent while every new API-owned worker is closed."""
    from api import loop_optimizer_v8 as api
    events = []

    async def close_controller():
        """Model a controller cleanup failure without touching a real controller."""
        events.append('controller')
        if controller_failure:
            raise RuntimeError('Fixture controller failure')

    monkeypatch.setattr(api, '_controller', SimpleNamespace(
        close=close_controller, store=SimpleNamespace(clear_read_cache=lambda: events.append('cache'))))
    monkeypatch.setattr(api, '_report_worker', SimpleNamespace(close=lambda: events.append('worker')))
    if controller_failure:
        with pytest.raises(RuntimeError, match='Fixture'):
            asyncio.run(api.shutdown())
    else:
        asyncio.run(api.shutdown())
    assert events == ['controller', 'worker', 'cache']
    assert api._report_worker is None
