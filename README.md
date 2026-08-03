# Probabilistic Early FPGA Timing Analysis

A drop-in timing-evaluation layer for VTR which replaces deterministic max-based propagation
during placement with a probabilistic formulation that models arrival times as random variables
and approximates max operations using Gaussian moment matching, preserving multi-path
competition.

Early-stage FPGA timing analysis is inherently uncertain: routing is incomplete, congestion is
only partially observed, and interconnect delays are coarsely estimated. Deterministic static
timing analysis nevertheless enforces hard path selection at every node, discarding competing
near-critical paths whose refined routing delays may later dominate. Retaining those alternatives
as a distribution yields a criticality signal that is less sensitive to placement-time
uncertainty and richer for optimization to act on.

The method augments prior placers rather than competing with them: it requires no modification to
the placement algorithm and remains compatible with existing optimizers. Across the VTR 7.0
benchmark suite it achieves a 6.05% geometric-mean reduction in critical path delay, reduces
seed-dependent CPD variation by 60.9%, and incurs a 5.11% total-flow runtime overhead.

> **When Timing is Uncertain, Infer: Probabilistic Early FPGA Timing Analysis for Robust
> Critical Path Optimization**
> Omar Sharif, Filip Wojcicki, Tarik Ourida, Wayne Luk, Christos-Savvas Bouganis
> Imperial College London — ASAP 2026

## Method

Deterministic STA propagates arrival times through the timing graph with a max operator, so each
node commits to a single maximum-delay incoming path. During early placement routing is
incomplete, several incoming paths can be close relative to the uncertainty in their delay
estimates, and a small perturbation flips the max-selected predecessor — suppressing
near-critical alternatives whose refined routing delays may later dominate.

Arrival times and edge delays are instead modelled as random variables, giving candidate arrival
`X_u(v)` from each predecessor `u`:

```
A(v) ~ N(μ_v, σ²_v)      d(u,v) ~ N(μ_uv, σ²_uv)      X_u(v) = A(u) + d(u,v)
```

`μ_uv` is the placement-time interconnect delay estimate used by VPR. `σ²_uv` is a proxy for
pre-routing uncertainty, not a calibrated process-variation model.

Reconvergence factors `A(v) = max_u X_u(v)` are approximated by Gaussian moment matching:

```
μ_v = μ₁Φ(α) + μ₂Φ(−α) + σφ(α),    α = (μ₁ − μ₂)/σ,    σ = √(σ₁² + σ₂²)
```

A node with `k` incoming candidates requires `k−1` pairwise updates and stores only constant-size
moment summaries. Dominance probabilities `P(X_i(v) = max_j X_j(v))` define a probabilistic
criticality `Crit_prob(e)`, which corrects the criticality consumed by the annealer:

```
Crit(e) = Crit_det(e) + α · ( Crit_prob(e) − Crit_det(e) )
```

**α** controls correction strength, **β** how long the correction remains active as the placer
cools, **λ** restricts it to timing-critical connections. Move generation and acceptance are
unchanged.

## Results — VTR 7.0 suite, N = 25 seeds

`(α, β) ∈ {(0.01, 2.0), (0.01, 4.0), (0.03, 2.0), (0.03, 4.0)}`, fixed λ = 0.35, on
`k6_frac_N10_frac_chain_mem32K_40nm`. The Ensemble Selector chooses the probabilistic run with
the best final routed CPD; the fallback variant additionally includes the baseline VPR run in the
selection set.

| Method | CPD Gain (%) ↑ | CPD Std, σ Reduction (%) ↓ | Iter. (%) ↓ | Wirelength (%) ↓ | Runtime Overhead (%) (Placement / Total) ↓ | Avg. Per-Seed CPD Gain (%) ↑ | Regression Rate (%) ↓ | Sign. Regr. Rate (%) ↓ | Avg. CPD Loss on Regression (%) ↓ |
|---|---|---|---|---|---|---|---|---|---|
| θ₁ | 1.64 | 25.7 | 6.7 | −2.24 | 26.33 / 4.37 | 0.86 | 43.2 | 33.1 | 4.70 |
| θ₂ | 1.26 | 7.2 | 9.4 | −2.02 | 26.08 / 4.41 | 0.72 | 40.7 | 32.4 | 5.49 |
| θ₃ | 1.97 | 24.4 | 8.6 | −2.39 | 26.61 / 4.07 | 1.09 | 38.1 | 29.9 | 4.59 |
| θ₄ | 1.34 | 25.3 | 9.3 | −1.71 | 27.31 / 4.99 | 0.91 | 41.6 | 31.8 | 4.48 |
| **Ensemble Selector** | **5.67** | **58.9** | **3.9** | **−3.06** | 28.40 / 5.11 | **4.63** | 16.5 | 9.3 | 2.18 |
| **Ensemble Selector (w/ fallback)** | **6.05** | **60.9** | **3.5** | **−2.83** | 28.40 / 5.11 | **5.02** | **0.0** | **0.0** | **0.00** |

No single θ is uniformly best; all four are selected across the suite, and individually they
still regress on some seeds. Robustness comes from the Ensemble Selector, while fallback
eliminates regressions entirely by choosing the baseline when no run improves CPD. Gains are
positive on all circuits, largest on routing-sensitive designs (`stereovision2` +21.76%,
`mkDelayWorker32B` +10.95%) and smallest on regular ones (`mcml` +0.93%, `LU8PEEng` +1.33%). The
overhead is dominated by the independent ensemble runs, which execute in parallel.

## Build

```bash
make -j$(nproc)          # binary at build/vpr/vpr
```

## Running one configuration

To run a configuration — here θ₃ (α = 0.03, β = 2.0, λ = 0.35):

```bash
build/vpr/vpr \
  vtr_flow/arch/timing/k6_frac_N10_frac_chain_mem32K_40nm.xml \
  vtr_flow/benchmarks/blif/alu4.blif \
  --seed 1 --disp off \
  --prob_timing_enable on --prob_timing_inject on --prob_timing_mode 4 \
  --prob_inject_mode quantile \
  --prob_timing_alpha 0.03 --prob_timing_beta 2.0 --prob_inject_lambda 0.35 \
  --prob_inject_quantile_start 0.15 --prob_inject_quantile_end 0.05
```

To run the default VPR baseline, omit the `--prob_*` flags.

## Running the ensemble evaluation

[run_proxy_deployment.py](run_proxy_deployment.py) sweeps seeds, runs the configuration portfolio
in parallel, and reports the CPD gain the Ensemble Selector achieves when the number of
configurations runnable in parallel is constrained — `--cores` lists those budgets, and a budget
equal to the portfolio size is the unconstrained case reported above.

```bash
python3 -u run_proxy_deployment.py --benchmark test --seeds 5 --max_workers 9 \
  --cores 2,4,8,16 --method pqt --quiet --pin_cores --timeout 86400 \
  --proxy 0 --baseline 1 --clear --results_dir PAPER_RESULTS 2>&1 | grep -v "^$"
```

`--method pqt` selects the timing-evaluation layer alone, sweeping
λ ∈ {0.05, 0.10, 0.20, 0.35} × α ∈ {0.01, 0.03} × β ∈ {2.0, 4.0} plus the baseline; θ₁–θ₄ are the
λ = 0.35 slice. Per-configuration CPD, wirelength, iterations and runtime land in
`<results_dir>/<benchmark>/eval_pqtonly_seed<N>/stage2_full_runs.csv`.
