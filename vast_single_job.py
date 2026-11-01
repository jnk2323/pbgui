"""Human-reviewed manual starts of one frozen optimizer, without an AI provider."""
from __future__ import annotations

import hashlib
import json
import time
from uuid import uuid4

from file_lock import advisory_file_lock
from logging_helpers import human_log as _log
from optimize_convergence import DEFAULTS, validate_config
from secure_files import ensure_private_directory, read_regular_file_nofollow
from vast_credentials import VastCredentialStore
from vast_jobs import config_name, job_id, write_json
from vast_provider import VastError
from vast_queue import queue_pause_generation, single_job_worker_id

SERVICE = 'Vast'
REVIEW_TTL = 15 * 60
RETENTION = 24 * 60 * 60
MAX_REVIEWS = 512


def _digest(value: dict) -> str:
    """Fingerprint public review data and private frozen input without exposing paths."""
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()).hexdigest()
    except (TypeError, ValueError):
        raise VastError('Prepared optimizer input or review data is invalid', 409) from None


def _owner(user_id: str) -> str:
    """Bind consent to the authenticated user, never a browser session credential."""
    if not isinstance(user_id, str) or not user_id:
        raise VastError('Sign in before reviewing an optimizer start', 401)
    return hashlib.sha256(user_id.encode()).hexdigest()


def _review_digest(review: dict) -> str:
    """Keep reviewed input immutable while execution status advances durably."""
    return _digest({key: review[key] for key in
                    ('id', 'owner', 'snapshot', 'previous_authorization', 'created_at', 'expires_at')})


def _review_root(queue):
    """Keep bounded durable reviews in private native cloud state."""
    return ensure_private_directory(queue.root / 'single-job-reviews')


def _read_review(queue, review_id: str) -> dict:
    """Read one generated review ID without following filesystem links."""
    root = _review_root(queue)
    path = root / (job_id(review_id) + '.json')
    if not path.exists():
        raise VastError('Start review expired; review this job again', 409)
    try:
        raw = read_regular_file_nofollow(path, root)
        if len(raw) > 65536:
            raise ValueError('review too large')
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('id') != review_id
                or value.get('status') not in {'pending', 'starting', 'started', 'failed'}
                or value.get('payload_digest') != _review_digest(value)):
            raise ValueError('invalid review')
        return value
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        _log(SERVICE, 'Manual start review could not be read', level='WARNING', meta={'error_type': type(exc).__name__})
        raise VastError('Start review is unreadable; no new rental authorized', 409) from None


def _snapshot(queue, identifier: str) -> tuple[dict, str | None]:
    """Check a ready, unleased ordinary input and bind its spending/control state."""
    from api.vast import RentalPreferences
    store = queue.store
    row = store.read(identifier)
    control = store.read(identifier, 'control.json')
    if (row.get('kind') in {'worker', 'calibration'} or row.get('loop_id')
            or row.get('status') != 'ready' or row.get('lease_id')
            or row.get('rental_state') != 'none' or control.get('stop') or control.get('cleanup')):
        raise VastError('Select a ready, unstarted normal cloud optimizer job', 409)
    for worker in queue.workers():
        if (worker.get('job_authorization') or {}).get('job_id') == identifier:
            raise VastError('This job already has a GPU rental; check its rental card', 409)
    state = queue.read()
    if not state.get('gpu_preferences'):
        raise VastError('Save GPU requirements and rental limits in Settings first', 422)
    try:
        settings = RentalPreferences.model_validate(state['gpu_preferences']).model_dump()
        convergence = validate_config(row.get('convergence_config') or {key: settings[key] for key in DEFAULTS})
    except ValueError:
        raise VastError('Saved GPU requirements or convergence settings are invalid', 422) from None
    credentials = VastCredentialStore(queue.root).metadata()
    if not credentials['configured']:
        raise VastError('Connect Vast.ai before starting this job', 409)
    config = store.read(identifier, 'input/optimize.json')
    intent = store.read(identifier, 'intent.json')
    name = config_name(row.get('config_name'))
    if row.get('iterations') != (config.get('optimize') or {}).get('iters'):
        raise VastError('Frozen optimizer iterations differ from the job record; prepare this job again', 409)
    previous = row.get('rental_authorization_id')
    if previous is not None:
        job_id(previous)
    backtest = config.get('backtest') or {}
    identity = {key: row.get(key) for key in ('id', 'config_name', 'iterations', 'workers', 'auto_cpu_workers', 'use_adg')}
    return {'job_id': identifier, 'name': name, 'iterations': row['iterations'],
            'exchanges': backtest.get('exchanges') or [], 'start_date': backtest.get('start_date'),
            'end_date': backtest.get('end_date'), 'settings': settings, 'convergence': convergence,
            'credential_generation': credentials['generation'],
            'queue_pause_generation': queue_pause_generation(state),
            'input_digest': _digest({'identity': identity, 'config': config, 'intent': intent})}, previous


def _public(review: dict) -> dict:
    """Expose only the concrete job and costs needed for the human decision."""
    snapshot = review['snapshot']
    settings = snapshot['settings']
    return {key: snapshot[key] for key in ('job_id', 'name', 'iterations', 'exchanges', 'start_date', 'end_date', 'convergence')} | {
        'review_id': review['id'], 'payload_digest': review['payload_digest'],
        'gpu_name': settings['gpu_name'], 'max_price_per_hour': settings['max_price'],
        'hours': settings['hours'], 'budget': settings['budget'], 'expires_at': review['expires_at']}


def preview(queue, identifier: str, user_id: str) -> dict:
    """Prepare a bounded owner-bound review; never rent, dispatch or rebind input."""
    identifier, owner = job_id(identifier), _owner(user_id)
    with advisory_file_lock(queue.root / '.queue-lock'):
        snapshot, previous = _snapshot(queue, identifier)
        with advisory_file_lock(queue.root / '.single-job-reviews-lock'):
            root = _review_root(queue)
            retained = []
            now = time.time()
            for path in root.glob('*.json'):
                review = _read_review(queue, path.stem)
                expiry = review['expires_at'] if review['status'] == 'pending' else review['updated_at'] + RETENTION
                if now >= expiry:
                    path.unlink()
                else:
                    retained.append(review)
            for review in retained:
                if (review['owner'] == owner and review['status'] == 'pending'
                        and review['snapshot'] == snapshot and review['previous_authorization'] == previous):
                    return _public(review)
            if len(retained) >= MAX_REVIEWS:
                raise VastError('Manual start review capacity reached; try again after old reviews expire', 429)
            review = {'id': uuid4().hex, 'owner': owner, 'snapshot': snapshot,
                      'previous_authorization': previous, 'status': 'pending',
                      'created_at': now, 'updated_at': now, 'expires_at': now + REVIEW_TTL}
            review['payload_digest'] = _review_digest(review)
            write_json(root / (review['id'] + '.json'), review)
            return _public(review)


def start(queue, identifier: str, user_id: str, review_id: str, payload_digest: str) -> dict:
    """Bind the selected frozen input to one durable rental identity after confirmation."""
    from vast_pool import select_offer
    identifier, review_id, owner = job_id(identifier), job_id(review_id), _owner(user_id)
    store = queue.store
    with advisory_file_lock(queue.root / ('.manual-job-' + identifier)):
        review = _read_review(queue, review_id)
        if review['owner'] != owner or review['snapshot']['job_id'] != identifier or review['payload_digest'] != payload_digest:
            raise VastError('This start review belongs to another job, user or payload', 409)
        worker_id = single_job_worker_id(review_id)
        if (store.root / 'jobs' / worker_id).exists():
            worker = store.read(worker_id)
            if worker['rental_state'] != 'none':
                return worker  # Even after cleanup: retries never create a second rental.
        if time.time() >= review['expires_at']:
            raise VastError('Start review expired; review this job again', 409)
        with advisory_file_lock(queue.root / '.queue-lock'):
            fresh, authorization = _snapshot(queue, identifier)
            if fresh != review['snapshot'] or authorization not in (review['previous_authorization'], review_id):
                raise VastError('Job, rental limits, account or queue controls changed; review this job again', 409)
            store.update(identifier, rental_authorization_id=review_id)
            review.update(status='starting', updated_at=time.time())
            write_json(_review_root(queue) / (review_id + '.json'), review)
        snapshot = review['snapshot']
        settings = snapshot['settings']
        scope = {'id': review_id, 'job_id': identifier, 'hours': settings['hours'], 'budget': settings['budget'],
                 'settings': settings, 'convergence': snapshot['convergence'],
                 'credential_generation': snapshot['credential_generation'],
                 'queue_pause_generation': snapshot['queue_pause_generation']}
        try:
            offer = select_offer(queue, settings, job_authorization=scope)
            with advisory_file_lock(queue.root / '.queue-lock'):
                fresh, authorization = _snapshot(queue, identifier)
                if fresh != snapshot or authorization != review_id:
                    raise VastError('Job or rental authorization changed; review this job again', 409)
                worker = queue.start(offer, settings['hours'], settings['budget'], 0, job_authorization=scope)
            review.update(status='started', updated_at=time.time(), worker_id=worker['id'])
            write_json(_review_root(queue) / (review_id + '.json'), review)
            return worker
        except Exception as exc:
            # Release only an unleased, unpaid input. Accepted/uncertain leases
            # retain their durable identity for native supervision and retries.
            try:
                with advisory_file_lock(queue.root / '.queue-lock'):
                    reservation = store.read(worker_id) if (store.root / 'jobs' / worker_id).exists() else None
                    intent = store.read(worker_id, 'intent.json') if reservation else {}
                    row = store.read(identifier)
                    if (not reservation or reservation['rental_state'] == 'none' and not intent.get('accepted_at')):
                        if row.get('status') == 'ready' and not row.get('lease_id') and row.get('rental_authorization_id') == review_id:
                            store.update(identifier, rental_authorization_id=review['previous_authorization'])
                        review.update(status='failed', updated_at=time.time())
                        write_json(_review_root(queue) / (review_id + '.json'), review)
            except Exception as recovery_error:
                _log(SERVICE, 'Manual optimizer start retains uncertain recovery state', level='ERROR',
                     meta={'error_type': type(recovery_error).__name__})
            if isinstance(exc, VastError):
                raise
            _log(SERVICE, 'Manual optimizer start failed', level='ERROR', meta={'error_type': type(exc).__name__})
            raise VastError('Optimizer start could not be confirmed; check its GPU rental card before trying again', 500) from None
