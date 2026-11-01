"""Approval-bound normal Vast optimizers using PBGui's native shared worker path."""
from __future__ import annotations

from pathlib import Path

from file_lock import advisory_file_lock
from optimize_convergence import DEFAULTS
from vast_credentials import VastCredentialStore
from vast_jobs import native_job_config
from vast_provider import VastError
from vast_queue import CloudQueue, queue_pause_generation, single_job_worker_id

SERVICE = 'AIChat'


def tool_specs() -> list[dict]:
    """Expose the same explicit cloud action to every AI provider."""
    return [{
        'name': 'propose_vast_optimizer_run',
        'description': 'Propose one normal PB8 optimizer on Vast.ai, without an AI Loop. Use a saved optimizer config; its iterations (including 20 million), strategy and dates are preserved. Explicitly choose stop_when_no_improvement. Uses saved GPU requirements, budget and hours, automatic GPU sizing and cleanup after this one job. Approval can rent a paid GPU immediately. Does not enable global Auto rent, change shared convergence preferences, or start local optimizers. Do not use local queue/start tools for Vast requests.',
        'schema': {'type': 'object', 'properties': {
            'name': {'type': 'string', 'maxLength': 128},
            'stop_when_no_improvement': {'type': 'boolean'},
        }, 'required': ['name', 'stop_when_no_improvement'], 'additionalProperties': False},
        'effect': 'execute', 'versions': ['v8'],
    }]


def _snapshot(service, name: str) -> dict:
    """Bind reviewed input, public rental limits and credential generation without secrets."""
    from ai_capabilities import AICapabilityError
    from api.vast import RentalPreferences
    source, overrides, fingerprint = service._current_pb8_bundle(name)
    if source is None:
        raise AICapabilityError('Save the PB8 optimizer config before proposing a Vast run')
    if overrides:
        raise AICapabilityError('Vast optimizer input does not support sparse override files')
    prepared = service._validate_pb8_config(name, source)
    opt = prepared.get('optimize') or {}
    workers = (opt.get('gpu') or {}).get('exact_workers') or opt.get('n_cpus') or 4
    # This validates the pinned cloud profile without probing local CUDA/Torch.
    native_job_config(prepared, opt.get('iters', 512), workers, False)
    queue = CloudQueue()
    queue_state = queue.read()
    stored = queue_state.get('gpu_preferences')
    if not stored:
        raise AICapabilityError('Save GPU requirements and rental limits in Vast.ai Settings first')
    try:
        settings = RentalPreferences.model_validate(stored).model_dump()
    except ValueError:
        raise AICapabilityError('Saved Vast rental limits are invalid') from None
    credentials = VastCredentialStore(queue.root).metadata()
    if not credentials['configured']:
        raise AICapabilityError('Connect Vast.ai before proposing a cloud optimizer')
    return {'source': prepared, 'source_digest': fingerprint, 'settings': settings,
            'credential_generation': credentials['generation'],
            'queue_pause_generation': queue_pause_generation(queue_state)}


async def propose(service, owner: str, conversation_id: str, args: dict) -> dict:
    """Create a review only; do not prepare jobs, change preferences or rent."""
    from ai_capabilities import AICapabilityError
    name = service._name(args.get('name'))
    stop = args.get('stop_when_no_improvement')
    if type(stop) is not bool:
        raise AICapabilityError('Choose stop_when_no_improvement explicitly')
    payload = await service._to_thread_uncancellable(checked, _snapshot, service, name)
    settings = payload['settings']
    convergence = {key: settings[key] for key in DEFAULTS}
    convergence['convergence_enabled'] = stop
    payload['convergence'] = convergence
    preview = {
        'action': 'run_vast_optimizer', 'name': name, 'execution': 'vast', 'job_count': 1,
        'iterations': payload['source'].get('optimize', {}).get('iters'),
        'gpu_name': settings['gpu_name'], 'max_price_per_hour': settings['max_price'],
        'budget': settings['budget'], 'hours': settings['hours'], 'convergence': convergence,
        'may_start_immediately': True,
        'effect': 'Run one normal optimizer on Vast.ai. Approval authorizes one paid rental within these limits and automatic cleanup after this job, even if the shared queue is paused. Other queued jobs stay paused. No AI Loop; global Auto rent and shared preferences stay unchanged.',
    }
    preview['changes'] = [{'path': key, 'kind': 'added', 'after': value}
                          for key, value in preview.items() if key not in {'action', 'name', 'effect'}]
    preview['changed_count'] = len(preview['changes'])
    return await service._create_custom_proposal(owner, conversation_id, 'run_vast_optimizer', name,
                                                 payload, preview, expected_digest=payload['source_digest'])


def execute(service, proposal) -> dict:
    """Execute a single reviewed input once, retaining native rental lifecycle safeguards."""
    from ai_capabilities import AICapabilityError
    from api import optimize_v8
    from vast_pool import select_offer
    payload = proposal.config or {}
    settings = payload['settings']
    queue = CloudQueue()
    store = queue.store
    # Approval execution already owns the durable journal/restart blocker. This
    # additional cross-process lock covers native preparation and reservation.
    with advisory_file_lock(queue.root / ('.ai-optimizer-' + proposal.id)):
        worker_id = single_job_worker_id(proposal.id)
        if (store.root / 'jobs' / worker_id).exists():
            worker = store.read(worker_id)
            if worker['rental_state'] != 'none':
                scope = store.read(worker_id, 'intent.json').get('job_authorization') or {}
                if scope.get('id') != proposal.id:
                    raise AICapabilityError('Cloud worker belongs to another approval')
                return _receipt(proposal, scope['job_id'], worker)
        fresh = _snapshot(service, proposal.name)
        bound_keys = ('source_digest', 'settings', 'credential_generation', 'queue_pause_generation')
        if any(fresh[key] != payload.get(key) for key in bound_keys):
            raise AICapabilityError('Optimizer config, Vast settings, account or queue pause changed after review; create a new proposal')
        source = payload['source']
        opt = source.get('optimize') or {}
        workers = (opt.get('gpu') or {}).get('exact_workers') or opt.get('n_cpus') or 4
        job = store.create_preparation(proposal.name, opt.get('iters', 512), workers, False,
                                       operation_id=proposal.id)
        scope = {'id': proposal.id, 'job_id': job['id'], 'hours': settings['hours'],
                 'budget': settings['budget'], 'convergence': payload['convergence'],
                 'settings': settings, 'credential_generation': payload['credential_generation'],
                 'queue_pause_generation': payload['queue_pause_generation']}
        if job['status'] == 'preparing':
            root = Path(optimize_v8.PBGDIR)
            job = store.prepare(proposal.name, source, payload['source_digest'], root / 'data/ohlcv',
                                root / 'data/coindata', optimize_v8._results_root(), opt.get('iters', 512),
                                workers, False, identifier=job['id'])
        if job['status'] != 'ready':
            raise AICapabilityError('Approved cloud input is no longer ready; inspect the job before a new proposal')
        # Fail closed on simultaneous saved-input/account changes before renting.
        offer = select_offer(queue, settings, job_authorization=scope)
        with optimize_v8._config_lock():
            fresh = _snapshot(service, proposal.name)
            if any(fresh[key] != payload.get(key) for key in bound_keys):
                raise AICapabilityError('Optimizer config, Vast settings, account or queue pause changed during preparation; no rental started')
            worker = queue.start(offer, settings['hours'], settings['budget'], 0, job_authorization=scope)
        return _receipt(proposal, job['id'], worker)


def _receipt(proposal, job_id: str, worker: dict) -> dict:
    """Distinguish rental authorization from proof that optimization is running."""
    return {'proposal_id': proposal.id, 'status': 'executed', 'action': 'run_vast_optimizer',
            'execution': 'vast', 'name': proposal.name, 'job_id': job_id,
            'worker_id': worker['id'], 'rental_state': worker['rental_state'],
            'worker_status': worker['status'], 'ai_loop': False}


def checked(function, *args):
    """Preserve native path-free cloud error messages at the capability boundary."""
    from ai_capabilities import AICapabilityError
    try:
        return function(*args)
    except VastError as exc:
        raise AICapabilityError(str(exc)) from exc
