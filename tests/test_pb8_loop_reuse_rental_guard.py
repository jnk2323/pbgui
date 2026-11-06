"""A resumed rental-bound loop must not silently buy a replacement GPU."""
from pathlib import Path
from pb8_loop_backend import LoopBackend


def test_reuse_only_never_opens_marketplace(monkeypatch):
    import vast_pool
    def unexpected(*args, **kwargs):
        raise AssertionError('reuse-only must not select a paid offer')
    monkeypatch.setattr(vast_pool, 'select_offer', unexpected)
    LoopBackend(Path('.'), None)._rent({'rental_reuse_only': True})


def test_handover_never_opens_marketplace(monkeypatch):
    import vast_pool
    def unexpected(*args, **kwargs):
        raise AssertionError('handover must not select a paid offer')
    monkeypatch.setattr(vast_pool, 'select_offer', unexpected)
    LoopBackend(Path('.'), None)._rent({'rental_handover': {'worker_id': 'a' * 32}})
