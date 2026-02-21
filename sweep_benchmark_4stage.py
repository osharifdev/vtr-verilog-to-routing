#!/usr/bin/env python3
"""
Coordinate Descent Parameter Sweep — Parametric for any benchmark.
Usage: python3 sweep_benchmark.py <benchmark>

Runs 4 stages sequentially:
Stage 1: lambda  in [0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05]
Stage 2: beta    in [0.25, 0.5, 1.0, 2.0, 3.0, 5.0]
Stage 3: alpha   in [0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0]
Stage 4: huber   in [10, 20, 30, 50, 75, 100, 200]
"""

import os, re, sys, subprocess, concurrent.futures
import pandas as pd
import numpy as np
from tqdm import tqdm

BENCH = sys.argv[1] if len(sys.argv) > 1 else "boundtop"

# Blif path lookup
BLIF_CANDIDATES = {
    "bgm":           "phase6_full_audit/bgm/bgm_baseline_s1/bgm.abc.blif",
    "mkPktMerge":    "phase6_full_audit/mkPktMerge/mkPktMerge_baseline_s1/mkPktMerge.abc.blif",
    "diffeq1":       "phase6_full_audit/diffeq1/diffeq1_baseline_s1/diffeq1.abc.blif",
    "diffeq2":       "phase6_full_audit/diffeq2/diffeq2_baseline_s4/diffeq2.abc.blif",
    "ch_intrinsics": "phase6_full_audit/ch_intrinsics/ch_intrinsics_baseline_s1/ch_intrinsics.abc.blif",
    "stereovision3": "phase6_full_audit/stereovision3/stereovision3_baseline_s1/stereovision3.abc.blif",
    "boundtop":      "phase6_full_audit/boundtop/boundtop_baseline_s1/boundtop.abc.blif",
}

VTR_ROOT = os.path.dirname(os.path.abspath(__file__))
VPR_BIN  = os.path.join(VTR_ROOT, "build/vpr/vpr")
ARCH     = os.path.join(VTR_ROOT, "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")

if BENCH not in BLIF_CANDIDATES:
    print(f"Error: unknown benchmark {BENCH}")
    sys.exit(1)

BLIF_SRC = os.path.join(VTR_ROOT, BLIF_CANDIDATES[BENCH])
assert os.path.exists(BLIF_SRC), f"blif not found: {BLIF_SRC}"

OUT_ROOT = os.path.join(VTR_ROOT, f"{BENCH}_sweep_4stage")
WORKERS  = 45
SEEDS    = [1, 2, 3, 4, 5]

STAGE1_LAMBDAS = [0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05]
STAGE2_BETAS   = [0.25, 0.5, 1.0, 2.0, 3.0, 5.0]
STAGE3_ALPHAS  = [0.0, 0.05, 0.1, 0.3, 0.5, 0.7, 1.0]
STAGE4_HUBERS  = [10, 20, 30, 50, 75, 100, 200]


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
    label, seed, params, run_dir = task
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
            "--prob_inject_lambda", str(params["lambda"]),
            "--prob_dist_func", "huber", "--prob_huber_delta", str(params["huber"]),
            "--prob_timing_beta", str(params["beta"]),
            "--prob_timing_alpha", str(params["alpha"]),
        ]

    try:
        with open(log_file, "w") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=600, cwd=run_dir)
        cpd = parse_cpd(log_file)
    except Exception:
        cpd = None
    return (label, seed, params.get("sweep_val"), cpd)


def run_stage(stage_name, sweep_key, sweep_values, fixed_params, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tasks = []

    for seed in SEEDS:
        tasks.append(("baseline", seed, {"sweep_val": None}, os.path.join(out_dir, f"baseline_s{seed}")))

    for val in sweep_values:
        p = dict(fixed_params)
        p[sweep_key] = val
        p["sweep_val"] = val
        vstr = str(val).replace(".", "p")
        tasks.append(("adaptive", seed, p, os.path.join(out_dir, f"{sweep_key}{vstr}_s{seed}")))

    # Bug in above loop: only last seed was appended. Fixed:
    tasks = []
    for seed in SEEDS:
        tasks.append(("baseline", seed, {"sweep_val": None}, os.path.join(out_dir, f"baseline_s{seed}")))
    for val in sweep_values:
        vstr = str(val).replace(".", "p")
        for seed in SEEDS:
            p = dict(fixed_params)
            p[sweep_key] = val
            p["sweep_val"] = val
            tasks.append(("adaptive", seed, p, os.path.join(out_dir, f"{sweep_key}{vstr}_s{seed}")))

    total = len(tasks)
    print(f"\n=== {stage_name}: sweep {sweep_key} over {sweep_values} ===")
    print(f"  Fixed: {fixed_params}")

    results = []
    workers = WORKERS
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(run_vpr, t): t for t in tasks}
        for fut in tqdm(concurrent.futures.as_completed(futures), total=total, desc=stage_name):
            label, seed, sv, cpd = fut.result()
            results.append({"label": label, "seed": seed, "sweep_val": sv, "cpd_ns": cpd})

    df = pd.DataFrame(results)
    df.to_csv(os.path.join(out_dir, "raw.csv"), index=False)

    baseline = df[df.label == "baseline"][["seed", "cpd_ns"]].rename(columns={"cpd_ns": "cpd_baseline"})
    merged = df[df.label == "adaptive"].merge(baseline, on="seed")
    merged["gain_pct"] = (merged["cpd_baseline"] - merged["cpd_ns"]) / merged["cpd_baseline"] * 100

    # For 0 progression criteria: we need gain >= 0 for ALL seeds.
    # Group by sweep_val and check if min(gain_pct) >= 0
    summary = merged.groupby("sweep_val")["gain_pct"].agg(
        mean_gain="mean", std_gain="std", min_gain="min", max_gain="max",
        regressions=lambda x: (x < 0).sum()
    ).reset_index()

    summary.to_csv(os.path.join(out_dir, "summary.csv"), index=False)
    print(summary.to_string(index=False, float_format=lambda x: f"{x:+.3f}"))

    # Select best val based on: 1. Fewest regressions, 2. Max mean gain
    best_row = summary.sort_values(["regressions", "mean_gain"], ascending=[True, False]).iloc[0]
    best_val = best_row["sweep_val"]
    print(f"\n★ Best {sweep_key}: {best_val} → mean={best_row['mean_gain']:+.3f}%  reg={int(best_row['regressions'])}/5")
    return best_val


def main():
    os.makedirs(OUT_ROOT, exist_ok=True)
    
    # Baseline for all stages
    print(f"\nStarting {BENCH} Coordinate Descent Sweep...")

    # Stage 1: Lambda
    best_lambda = run_stage("Stage 1: Lambda", "lambda", STAGE1_LAMBDAS, 
                           {"beta": 1.0, "alpha": 0.0, "huber": 50.0},
                           os.path.join(OUT_ROOT, "stage1_lambda"))
    
    # Stage 2: Beta
    best_beta = run_stage("Stage 2: Beta", "beta", STAGE2_BETAS,
                         {"lambda": best_lambda, "alpha": 0.0, "huber": 50.0},
                         os.path.join(OUT_ROOT, "stage2_beta"))

    # Stage 3: Alpha
    best_alpha = run_stage("Stage 3: Alpha", "alpha", STAGE3_ALPHAS,
                          {"lambda": best_lambda, "beta": best_beta, "huber": 50.0},
                          os.path.join(OUT_ROOT, "stage3_alpha"))

    # Stage 4: Huber delta
    best_huber = run_stage("Stage 4: Huber Delta", "huber", STAGE4_HUBERS,
                          {"lambda": best_lambda, "beta": best_beta, "alpha": best_alpha},
                          os.path.join(OUT_ROOT, "stage4_huber"))

    print(f"\nFinal Summary for {BENCH}:")
    print(f"  lambda: {best_lambda}")
    print(f"  beta:   {best_beta}")
    print(f"  alpha:  {best_alpha}")
    print(f"  huber:  {best_huber}")


if __name__ == "__main__":
    main()
