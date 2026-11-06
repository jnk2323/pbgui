# c8c33e4a validation failure

All three CPU validation processes returned 0, each with 30 analysis reports. Restarts did not cancel them. Files live in `data/bt_v8_queue/state/`, `data/logs/backtests_v8/`, and `/root/software/pb8/backtests/pbgui/loop_<id>_<operation>/suite_runs/`.

PB8 base f6ffee98c3abe22b321a9b14641bc3133d7855cd built the real exchange universe as `[ds.exchange for ds in datasets.values()]` when both combined and individual data were prepared. For this suite this produced `combined,binance`, silently dropping bybit from the combined dataset's contributing exchanges. The binance-only windows were consequently sent to the combined dataset (2021-12-22 through 2026-10-25), not their individual binance dataset (2019-10-04 through 2026-11-04).

Evidence: log `data/logs/backtests_v8/0d2e23da-e4c6-49ad-af40-80af465ba8f3.log` lines 11-20 lists both datasets; reports w01-w09 have completion_ratio 0 and null effective dates. Full-range report has completion_ratio 0.9955396966993756 and effective_end_date 2026-10-24T23:59:00Z. Same on e2fecd45. Controller correctly refused incomplete evidence at pb8_loop_controller.py evaluate. Do not loosen its completeness threshold.

Fix is installed in `/root/software/pb8/src/suite_runner.py`; distributable native diff including regression test is `patches/pb8-suite-mixed-exchanges.patch`. After PB8 upgrades verify/reapply it (git apply --check then git apply from PB8 root). This PBGUI commit also records incomplete window labels, ratios and effective timestamps in exact assessments for future diagnosis. The native patch unions real contributors from combined data and individual exchanges; no dates, cache contents or strategy settings are changed.

Recovery: stopped API, backed up owner state to data/_repair_backups/20261106-validation-routing, retained optimizer job and candidates, archived old CPU jobs in validation_repair_archive, reserved fresh baseline + the same three selected validations. Same loop, same limits (3 x 12h, $4). Forced rental_reuse_only=true and vast.auto_rent=false. No new instance authorized or rented. b10b752c remains stopped/select; no duplicate active loop.
