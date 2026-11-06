# Loop stop/resume starvation

Live py-spy capture identified the evidence reader for snapshot
61e68fa4769c412fa1727e9aacf5e646, pointer `/data/evidence/candidates`,
offset 0. It eagerly expanded all 1272 candidates (including complete configs)
before returning the first page. Repeated subtree serialization/tokenization and
whole-response pagination kept the evidence thread busy. `owned_thread` joined
that worker on cancellation, while `tick` retained the `.controller` flock.
Resume correctly rejected that lock. A restart released the OS flock, but the
persisted snapshot and repeatable broad tool request recreated the expensive read;
the lock itself was not surviving process death.

Optional evidence reads now stream only through the requested page. Subtree size
checks stop serializing after a small prefix before exact token measurement.
Read cancellation signals a session event, prevents result publication and
cancels ledger charges under their authoritative lock. Only the read-only evidence
worker can detach on timeout; native launch/collection operations still join.
Control-generation checks reject stale failure writes. Startup reconciliation
retains orphaned billing reservations and pauses for review rather than replaying.
API actions have a 60s bound and expensive resume validation runs off the event loop.

`rental_reuse_only` is a durable per-loop restriction: `_rent` never purchases a
replacement for such a loop. Existing worker dispatch remains unchanged.
