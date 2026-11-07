# Proxy strategy drawdown: terminal account-floor clipping

## Evidence and scope

PB8 fix: `jnk2323/passivbot` branch `fix/vast-rental-hours-cap`, commit `a1d52a9b0`.
Reviewable diff: `patches/pb8-proxy-raw-strategy-drawdown.patch`.
Installed on the local PB8 source checkout; no API restart or GPU rental performed.
No historical job inputs/results changed. Future packaging must pin the fixed PB8
commit, not the failed job's b9d6692 runtime.

Confirmed semantic bug (not a measured attribution of all rank drift):
- `passivbot-rust/src/gpu/mps_trailing_martingale_multicoin.metal:5550`
  clamps effective account equity to the liquidation floor, previously feeding
  `run_peak`, `max_dd` and daily drawdown. CUDA compiles these same Metal sources
  through `src/optimization/gpu/cuda_kernel.py:90`.
- `src/optimization/gpu/metrics.py:2294` exports `max_dd` as
  `drawdown_worst_strategy_eq`. Legacy `drawdown_worst_usd` is a direct alias
  at line 31, not an independent wallet drawdown calculation.
- Exact `passivbot-rust/src/backtest_hsl_analysis.rs:16` records starting balance
  plus net realized PNL plus marked UPNL, retaining terminal losses even below
  the liquidation floor. `backtest.rs:4603` computes running-peak drawdowns;
  `backtest.rs:4282` reduces their maximum. `python.rs:1333` publishes it.
- Exact legacy account/USD drawdown is calculated separately by
  `analysis.rs:699` and `analysis.rs:1665`, on the account equity series.
  Legacy USD aliasing in the proxy is not interchangeable with exact account
  USD semantics on liquidation and remains outside this strategy-metric fix.

The failed job saved an actual discrepancy in its sole Pareto candidate:
`metrics.gpu_validation.constraint_diagnostics` drawdown proxy 0.9991897344589233
versus exact 1.0006831306167494. This confirms raw terminal losses exceed 100%
where account-floor clipping cannot represent the exact result. It does not
prove this mechanism alone explains rho=0.562124 across the validation set.

## Version comparison

Both b9d6692 and local f6ffee98 have the same account-floor drawdown bug.
Exact strategy series and GPU metric alias definitions do not differ between
these commits. There ARE other kernel changes (decimal/order-price arithmetic,
parameter layouts) and CUDA long lowering; they must not be confused with a
change to the drawdown definition. `git apply --check` of the supplied patch
against extracted b9d6692 source files passes.

## Change

Seven risk blocks across five kernels (EMA anchor and trailing martingale,
single/directional/multicoin/chunked variants) now use raw equity for strategy
running peak and drawdown. Liquidation permission, account-floor series,
BTC risk, day end equity and ADG calculations are unchanged. No drift thresholds
were modified. Dual-side aggregate sum/clamp approximation in service.py is
not an exact portfolio reconstruction and is not addressed by this fix.

## CPU reproduction and verification

`tests/optimization/test_gpu_strategy_drawdown_cpu.py` extracts and compiles
the actual seven shader risk blocks with g++, then executes identical four
candidate equity paths against the exact running-peak formula. For paths
[100,120,110,terminal], terminal values 90,4,-2,-20 yield exact/fixed drawdowns
0.25,0.9666667,1.0166667,1.1666667. The prior floor=5 calculation returns
0.25,0.9583333,0.9583333,0.9583333, collapsing the last three candidate ranks.
This is a minimal kernel-level reproduction, NOT a full backtest of the saved
32 configs. No local NVIDIA GPU is available.

Commands from /root/software/pb8:

```
PYTHONPATH=src /root/software/venv_pb8/bin/python -m pytest tests/optimization/test_gpu_strategy_drawdown_cpu.py -o addopts='' -q
# 6 passed, 10 skipped (non-portfolio shader files); seven blocks execute
PYTHONPATH=src /root/software/venv_pb8/bin/python -m pytest tests/optimization/test_gpu_cuda.py -o addopts='' -q
# 27 passed, 40 skipped (GPU-dependent)
curl http://127.0.0.1:8000/health
# HTTP 200
```

## Rank impact and next evidence

The demonstrated liquidation-floor ties are removed; non-liquidated paths are
unchanged. No numerical forecast of real-job rho is justified without paired
GPU/exact evaluations of the same candidates. Terminal fill fees, trading path
approximation and dual-side composition can still cause differences. A paid
smoke test is pending, not launched: cap total spend at USD 3.50 and pin a1d52a9b0;
actual cost requires a current offer price and test duration. Do not claim the
run-level halt is resolved until that paired comparison is measured.
