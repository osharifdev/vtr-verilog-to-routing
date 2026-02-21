#!/usr/bin/env python3
"""
Generic 2D Micro-Sweep: alpha × lambda grid for any benchmark.

Usage: python3 micro_sweep.py <benchmark>

Supported: diffeq1, diffeq2, ch_intrinsics, stereovision3, boundtop

Grid:  alpha  ∈ {0.0, 0.1, 0.3, 0.5, 0.7, 1.0}
       lambda ∈ {0.005, 0.01, 0.02, 0.03, 0.04, 0.05}
Fixed: beta=1.0, huber=50.0, cap=0.05

Reports all configs achieving 0/5 regressions, sorted by mean gain.
"""

import os, re, sys, subprocess, concurrent.futures
import pandas as pd
from tqdm import tqdm

BENCH = sys.argv[1] if len(sys.argv) > 1 else "diffeq1"

# Blif path lookup
BLIF_CANDIDATES = {
    "diffeq1":       "phase6_full_audit/diffeq1/diffeq1_baseline_s1/diffeq1.abc.blif",
    "diffeq2":       "phase6_full_audit/diffeq2/diffeq2_baseline_s4/diffeq2.abc.blif",
    "ch_intrinsics": "phase6_full_audit/ch_intrinsics/ch_intrinsics_baseline_s1/ch_intrinsics.abc.blif",
    "stereovision3": "phase6_full_audit/stereovision3/stereovision3_baseline_s1/stereovision3.abc.blif",
    "boundtop":      "phase6_full_audit/boundtop/boundtop_baseline_s1/boundtop.abc.blif",
}


VTR_ROOT = os.path.dirname(os.path.abspath(__file__))
VPR_BIN  = os.path.join(VTR_ROOT, "build/vpr/vpr")
ARCH     = os.path.join(VTR_ROOT, "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BLIF_SRC = os.path.join(VTR_ROOT, BLIF_CANDIDATES[BENCH])
assert os.path.exists(BLIF_SRC), f"blif not found: {BLIF_SRC}"

OUT_DIR = os.path.join(VTR_ROOT, f"{BENCH}_microsweep")
WORKERS = 45
SEEDS   = [1, 2, 3, 4, 5]

ALPHAS  = [0.0, 0.1, 0.3, 0.5, 0.7, 1.0]
LAMBDAS = [0.005, 0.01, 0.02, 0.03, 0.04, 0.05]
FIXED_BETA  = 1.0
FIXED_HUBER = 50.0


def parse_cpd(log_path):
    cpd = None
    try:
        with open(log_path) as f:
            for line in f:
                m = re.search(r'Final critical path delay \(least slack\):\s+([\d.]+)\s+ns', line)
                if m:
                    cpd = float(m.group(1))
    except Exception:
        pass
    return cpd


def run_vpr(task):
    label, seed, lam, alpha, run_dir = task
    os.makedirs(run_dir, exist_ok=True)
    log_file = os.path.join(run_dir, "vpr.out")
    base_cmd = [VPR_BIN, ARCH, BLIF_SRC,
                "--route_chan_width", "300", "--disp", "off", "--seed", str(seed)]
    if label == "baseline":
        cmd = base_cmd
    else:
        cmd = base_cmd + [
            "--prob_timing_enable", "on", "--prob_timing_mode", "4",
            "--prob_timing_inject", "on", "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", str(lam),
            "--prob_dist_func", "huber", "--prob_huber_delta", str(FIXED_HUBER),
            "--prob_timing_beta", str(FIXED_BETA),
            "--prob_timing_alpha", str(alpha),
        ]
    try:
        with open(log_file, "w") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=600, cwd=run_dir)
        cpd = parse_cpd(log_file)
    except Exception:
        cpd = None
    return (label, seed, lam, alpha, cpd)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    tasks = []
    for seed in SEEDS:
        tasks.append(("baseline", seed, 0, 0, os.path.join(OUT_DIR, f"baseline_s{seed}")))
    for alpha in ALPHAS:
        for lam in LAMBDAS:
            astr = str(alpha).replace(".", "p")
            lstr = str(lam).replace(".", "p")
            for seed in SEEDS:
                tasks.append(("adaptive", seed, lam, alpha,
                              os.path.join(OUT_DIR, f"a{astr}_l{lstr}_s{seed}")))

    total = len(tasks)
    print(f"\n=== {BENCH} Micro-Sweep: {len(ALPHAS)}×{len(LAMBDAS)} alpha×lambda grid ===")
    print(f"  alpha:  {ALPHAS}")
    print(f"  lambda: {LAMBDAS}")
    print(f"  Total runs: {total}  Workers: {WORKERS}\n")

    results = []
    workers = WORKERS
    while workers >= 4:
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as exe:
                futures = {exe.submit(run_vpr, t): t for t in tasks}
                for fut in tqdm(concurrent.futures.as_completed(futures), total=total, desc=BENCH):
                    label, seed, lam, alpha, cpd = fut.result()
                    results.append({"label": label, "seed": seed,
                                    "lambda": lam, "alpha": alpha, "cpd_ns": cpd})
            break
        except Exception as e:
            print(f"Pool failed at w={workers}: {e} — retrying with {workers-8}")
            results.clear()
            workers -= 8

    df = pd.DataFrame(results)
    df.to_csv(os.path.join(OUT_DIR, "raw.csv"), index=False)

    baseline = df[df.label == "baseline"][["seed", "cpd_ns"]].rename(columns={"cpd_ns": "cpd_baseline"})
    merged = df[df.label == "adaptive"].merge(baseline, on="seed")
    merged["gain_pct"] = (merged["cpd_baseline"] - merged["cpd_ns"]) / merged["cpd_baseline"] * 100

    summary = merged.groupby(["alpha", "lambda"])["gain_pct"].agg(
        mean_gain="mean", std_gain="std", min_gain="min", max_gain="max",
        regressions=lambda x: (x < 0).sum()
    ).reset_index().sort_values(["regressions", "mean_gain"], ascending=[True, False])

    summary.to_csv(os.path.join(OUT_DIR, "summary.csv"), index=False)

    print(f"\n=== {BENCH} Micro-Sweep Results (sorted: fewest regressions, then best mean) ===")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

    zero_reg = summary[summary["regressions"] == 0]
    print(f"\n=== Configs with 0/5 regressions ===")
    if zero_reg.empty:
        print(f"  NONE — {BENCH} cannot achieve 0 regressions at these parameter values")
        best_1reg = summary[summary["regressions"] == 1]
        if not best_1reg.empty:
            best = best_1reg.iloc[0]
            print(f"  Best 1-reg config: alpha={best['alpha']}, lambda={best['lambda']} → {best['mean_gain']:+.3f}%")
    else:
        print(zero_reg.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
        best = zero_reg.iloc[0]
        print(f"\n★ {BENCH} optimal: alpha={best['alpha']}, lambda={best['lambda']} → {best['mean_gain']:+.3f}%  (0 regressions)")


if __name__ == "__main__":
    main()
