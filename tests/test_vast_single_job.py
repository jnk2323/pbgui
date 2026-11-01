"""Offline manual single-job review, ownership, spending and recovery contracts."""
import copy
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import vast
from api.auth import require_auth
from api.vast import RentalPreferences
from optimize_convergence import DEFAULTS
from vast_credentials import VastCredentialStore
from vast_jobs import JobStore, write_json
from vast_provider import VastError
from vast_queue import CloudQueue, single_job_worker_id
import vast_single_job as single


@pytest.fixture
def manual(tmp_path, monkeypatch):
    """Keep frozen jobs, review records and all provider effects in temporary storage."""
    from test_vast_pool import add_row
    import vast_pool
    queue = CloudQueue(JobStore(tmp_path / 'vast'))
    target, other = add_row(queue, 10), add_row(queue, 11)
    for identifier in (target, other):
        queue.store.update(identifier, config_name='hype_' + identifier[-2:], iterations=20_000_000)
        config = queue.store.read(identifier, 'input/optimize.json')
        config['optimize']['iters'] = 20_000_000
        write_json(queue.store.directory(identifier) / 'input/optimize.json', config)
    settings = RentalPreferences(gpu_name='RTX 3060', hours=24, budget=5, max_price=.3,
                                 convergence_enabled=True).model_dump()
    queue.update(paused=True, gpu_preferences=settings)
    account = {'generation': 1, 'configured': True}
    monkeypatch.setattr(VastCredentialStore, 'metadata', lambda self: dict(account))
    calls = []

    def offer(queue, settings, *, job_authorization):
        """Assert only the selected job influences the provider search."""
        assert [row['id'] for row in queue.waiting(authorization_id=job_authorization['id'])] == [target]
        assert [row['id'] for row in queue.waiting()] == [other]
        calls.append(('offer', copy.deepcopy(job_authorization)))
        return {'id': 123}

    def rent(offer, hours, budget, idle_seconds, *, job_authorization):
        """Persist a native reservation while replacing all real rental effects."""
        assert idle_seconds == 0 and hours == 24 and budget == 5
        calls.append(('rent', copy.deepcopy(job_authorization)))
        worker = {'id': single_job_worker_id(job_authorization['id']), 'kind': 'worker',
                  'status': 'provisioning', 'rental_state': 'creation_pending',
                  'job_authorization': job_authorization}
        queue.store.publish(worker, intent={'job_authorization': job_authorization})
        return worker

    monkeypatch.setattr(vast_pool, 'select_offer', offer)
    monkeypatch.setattr(queue, 'start', rent)
    return queue, target, other, calls, account


@pytest.mark.parametrize('ai_prepared', [False, True])
def test_manual_review_and_start_preserve_queue_and_replay_once(manual, ai_prepared):
    """Ordinary and failed AI inputs use the same manual path, without any AI context."""
    queue, target, other, calls, account = manual
    if ai_prepared:
        queue.store.update(target, rental_authorization_id='a' * 32)
    before = queue.read()
    review = single.preview(queue, target, 'manual-user')
    assert review == single.preview(queue, target, 'manual-user')
    assert calls == []
    assert review['iterations'] == 20_000_000 and review['budget'] == 5 and review['hours'] == 24
    assert review['convergence']['convergence_enabled'] is True
    assert queue.store.read(target).get('rental_authorization_id') == ('a' * 32 if ai_prepared else None)
    result = single.start(queue, target, 'manual-user', review['review_id'], review['payload_digest'])
    assert result['rental_state'] == 'creation_pending'
    assert [row[0] for row in calls] == ['offer', 'rent']
    assert calls[-1][1]['job_id'] == target
    assert queue.store.read(other)['status'] == 'ready'
    assert queue.read() == before  # Paused queue and global Auto rent remain unchanged.
    assert single.start(queue, target, 'manual-user', review['review_id'], review['payload_digest']) == result
    assert len(calls) == 2
    with pytest.raises(VastError, match='already has a GPU rental'):
        single.preview(queue, target, 'manual-user')


@pytest.mark.parametrize('changed', ['owner', 'job', 'digest', 'input', 'budget', 'account', 'pause', 'expired', 'stopped', 'loop'])
def test_changed_manual_review_cannot_rent(manual, changed):
    """Bind every cost-relevant and input/ownership dependency before charging."""
    queue, target, other, calls, account = manual
    review = single.preview(queue, target, 'owner')
    owner, posted_id, digest = 'owner', target, review['payload_digest']
    if changed == 'owner':
        owner = 'someone-else'
    elif changed == 'job':
        posted_id = other
    elif changed == 'digest':
        digest = '0' * 64
    elif changed == 'input':
        path = queue.store.directory(target) / 'input/optimize.json'
        config = queue.store.read(target, 'input/optimize.json')
        config['optimize']['iters'] = 512
        write_json(path, config)
    elif changed == 'budget':
        queue.update(gpu_preferences={**queue.read()['gpu_preferences'], 'budget': 10})
    elif changed == 'account':
        account['generation'] += 1
    elif changed == 'pause':
        queue.action('pause')
    elif changed == 'expired':
        row = single._read_review(queue, review['review_id'])
        row['expires_at'] = time.time() - 1
        row['payload_digest'] = single._review_digest(row)
        digest = row['payload_digest']
        write_json(single._review_root(queue) / (row['id'] + '.json'), row)
    elif changed == 'stopped':
        queue.store.control(target, 'stop')
    else:
        queue.store.update(target, loop_id='a' * 32)
    with pytest.raises(VastError):
        single.start(queue, posted_id, owner, review['review_id'], digest)
    assert calls == []
    assert queue.store.read(target).get('rental_authorization_id') is None


def test_failed_unpaid_manual_start_restores_input_authorization(manual, monkeypatch):
    """An unavailable offer leaves the original frozen job reusable and rents nothing."""
    import vast_pool
    queue, target, other, calls, account = manual
    queue.store.update(target, rental_authorization_id='a' * 32)
    review = single.preview(queue, target, 'owner')

    def unavailable(*args, **kwargs):
        """Simulate the provider having no eligible GPU, without network access."""
        raise VastError('No eligible GPU', 409)

    monkeypatch.setattr(vast_pool, 'select_offer', unavailable)
    with pytest.raises(VastError, match='No eligible GPU'):
        single.start(queue, target, 'owner', review['review_id'], review['payload_digest'])
    assert queue.store.read(target)['rental_authorization_id'] == 'a' * 32
    assert queue.store.read(target)['status'] == 'ready'
    assert calls == []


def test_manual_preview_capacity_and_expiry_are_bounded(manual, monkeypatch):
    """Repeated unused reviews cannot grow native state indefinitely."""
    queue, target, other, calls, account = manual
    monkeypatch.setattr(single, 'MAX_REVIEWS', 1)
    first = single.preview(queue, target, 'owner')
    with pytest.raises(VastError, match='capacity'):
        single.preview(queue, other, 'owner')
    row = single._read_review(queue, first['review_id'])
    row['expires_at'] = time.time() - 1
    row['payload_digest'] = single._review_digest(row)
    write_json(single._review_root(queue) / (row['id'] + '.json'), row)
    assert single.preview(queue, other, 'owner')['job_id'] == other
    assert calls == []


def test_tampered_persisted_manual_review_cannot_change_the_reviewed_budget(manual):
    """A stale payload digest never approves later edits to the durable review."""
    queue, target, other, calls, account = manual
    review = single.preview(queue, target, 'owner')
    row = single._read_review(queue, review['review_id'])
    row['snapshot']['settings']['budget'] = 50
    write_json(single._review_root(queue) / (row['id'] + '.json'), row)
    with pytest.raises(VastError, match='unreadable'):
        single.start(queue, target, 'owner', review['review_id'], review['payload_digest'])
    assert calls == []


def test_manual_start_routes_require_concrete_cost_confirmation(manual, monkeypatch):
    """Exercise authenticated API routes with a real manual review and inert provider."""
    queue, target, other, calls, account = manual
    monkeypatch.setattr(vast, 'CloudQueue', lambda: queue)
    app = FastAPI()
    app.include_router(vast.router, prefix='/api/vast')
    app.dependency_overrides[require_auth] = lambda: SimpleNamespace(user_id='manual-owner')
    with TestClient(app) as client:
        review_response = client.post(f'/api/vast/jobs/{target}/start-preview')
        assert review_response.status_code == 200 and review_response.headers['cache-control'] == 'no-store'
        review = review_response.json()
        payload = {key: review[key] for key in ('review_id', 'payload_digest')}
        assert client.post(f'/api/vast/jobs/{target}/start', json=payload).status_code == 422
        assert calls == []
        payload['accept_rental_and_cleanup'] = True
        response = client.post(f'/api/vast/jobs/{target}/start', json=payload)
        assert response.status_code == 202
        assert client.post(f'/api/vast/jobs/{target}/start', json=payload).json() == response.json()
        assert [row[0] for row in calls] == ['offer', 'rent']
        app.dependency_overrides.clear()
        assert client.post(f'/api/vast/jobs/{other}/start-preview').status_code == 401
