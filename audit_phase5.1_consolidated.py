import os
import subprocess
import time
import re
import concurrent.futures
from pathlib import Path
import pandas as pd
import argparse

try:
    from tqdm import tqdm
except ImportError:
    tqdm = lambda x, **kwargs: x

# --- Configuration ---
VTR_ROOT = "/home/os717/vtr-verilog-to-routing"
RUN_VTR_FLOW = os.path.join(VTR_ROOT, "vtr_flow/scripts/run_vtr_flow.py")
ARCH_FILE = os.path.join(VTR_ROOT, "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")

LAMBDA = 0.02
DELTA = 50.0

def run_single_flow(bench_name, seed, mode, out_dir):
    run_name = f"{bench_name}_{mode}_s{seed}"
    run_dir = os.path.join(out_dir, bench_name, run_name)
    os.makedirs(run_dir, exist_ok=True)
    
    bench_path = os.path.join(VTR_ROOT, "vtr_flow/benchmarks/verilog", f"{bench_name}.v")
    
    # 1. Run flow until ABC to get the pre-VPR BLIF
    flow_cmd = [
        "python3", RUN_VTR_FLOW,
        bench_path, ARCH_FILE,
        "-start", "odin",
        "-end", "abc",
        "-temp_dir", run_dir,
    ]
    subprocess.run(flow_cmd, capture_output=True)
    
    # 2. Call VPR binary directly
    vpr_bin = os.path.join(VTR_ROOT, "build/vpr/vpr")
    blif_path = os.path.join(run_dir, f"{bench_name}.abc.blif")
    if not os.path.exists(blif_path):
        blif_path = os.path.join(run_dir, f"{bench_name}.odin.blif")
        
    vpr_cmd = [
        vpr_bin, ARCH_FILE, blif_path,
        "--route_chan_width", "300",
        "--disp", "off",
        "--seed", str(seed)
    ]
    
    if mode == 'adaptive_huber':
        vpr_cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_mode", "4",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", str(LAMBDA),
            "--prob_dist_func", "huber",
            "--prob_huber_delta", str(DELTA)
        ]

    try:
        result = subprocess.run(vpr_cmd, capture_output=True, text=True, cwd=run_dir)
        
        # Save output for debugging
        with open(os.path.join(run_dir, "vpr.out"), "w") as f:
            f.write(result.stdout)
            f.write(result.stderr)
            
        cpd = 0.0
        m = re.search(r"Final critical path delay \(least slack\):\s+([0-9.]+)\s+ns", result.stdout)
        if m:
            cpd = float(m.group(1))
        
        return bench_name, seed, mode, cpd
    except Exception as e:
        print(f"Exception for {run_name}: {str(e)}")
        return bench_name, seed, mode, 0.0

if __name__ == "__main__":
    BENCHMARKS = ["bgm", "ch_intrinsics", "mkPktMerge", "diffeq2", "diffeq1", "stereovision3", "boundtop"]
    SEEDS = [1, 2, 3, 4, 5]
    WORKERS = 32
    OUT_DIR = os.path.join(VTR_ROOT, "phase5.1_full_audit")

    os.makedirs(OUT_DIR, exist_ok=True)

    tasks = []
    for bench in BENCHMARKS:
        for seed in SEEDS:
            tasks.append((bench, seed, 'baseline'))
            tasks.append((bench, seed, 'adaptive_huber'))

    print(f"=== Phase 5.1 Consolidated Audit: {BENCHMARKS} ===")
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(run_single_flow, b, s, m, OUT_DIR) for b, s, m in tasks]
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(tasks), desc="VPR Runs"):
            results.append(future.result())

    df = pd.DataFrame(results, columns=['benchmark', 'seed', 'mode', 'cpd'])
    df.to_csv(os.path.join(OUT_DIR, "raw_results.csv"), index=False)
    
    pivot = df.pivot_table(index=['benchmark', 'seed'], columns='mode', values='cpd').reset_index()
    pivot = pivot[(pivot['baseline'] > 0) & (pivot['adaptive_huber'] > 0)]
    pivot['gain (%)'] = (pivot['baseline'] - pivot['adaptive_huber']) / pivot['baseline'] * 100

    summary = pivot.groupby('benchmark').agg({
        'baseline': 'mean',
        'adaptive_huber': 'mean',
        'gain (%)': ['mean', 'min', 'max']
    }).reset_index()

    pivot.to_csv(os.path.join(OUT_DIR, "results_per_seed.csv"), index=False)
    
    print("\n--- CONSOLIDATED SUMMARY (PHASE 5.1) ---")
    print(summary.to_string())
    
    # Save a markdown version for easy ingestion into the report
    with open(os.path.join(OUT_DIR, "summary.md"), "w") as f:
        f.write("# Phase 5.1 Consolidated Audit Summary\n\n")
        f.write(summary.to_markdown(index=False))
        f.write("\n\n## Per-Seed Details\n\n")
        f.write(pivot.to_markdown(index=False))

    print(f"\nAudit complete. Results saved to {OUT_DIR}")
