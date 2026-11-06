# Ready loop optimizer with no rental after rental-bound recovery

The ready optimizer `1458c7429d284bbca46bd92cc5ea56ec` was **not** a worker
record. Its state had no `kind`; the deleted worker `942dd17c89f2412498fb0bc372a4f722`
was a separate record. The shared pool excludes jobs belonging to a loop;
`CloudQueue.waiting()` defaults to `loop_id=None`. Its worker exclusion was not
the cause of this stall.

Loop rentals are launched by `LoopController` -> `LoopBackend.start()` ->
`_start_locked()` -> `_rent()`, using a scoped queue for the loop id.
The run had `rental_reuse_only=True`, no remaining active rental, and frozen
settings with budget $4. `_rent()` deliberately returned without marketplace
selection. Shared Auto rent changes cannot override this explicit safety fence.

Recovery requires renewed owner consent, not automatically dropping the fence.
For this incident consent was one rental, at most $3.50, max_rentals=1.
Under the loop file lock and shared `.execution` lock, verify no active workers,
update the loop frozen budget to $3.50/max_rentals=1, and call `_rent()` once
with a deep copy of the record excluding `rental_reuse_only`. Preserve that flag
in durable state: the controller can use the newly rented worker but cannot
silently buy a second one after failure or deletion. Do not retry if a reservation
or provider attempt already exists. Use native supervisors to provision/dispatch.

Regression coverage preserves both reuse-only and handover no-purchase fences.
