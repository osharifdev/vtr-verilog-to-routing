import subprocess
import re
import os
import concurrent.futures
from pathlib import Path
import time
import pandas as pd

# Robustness Validation Configuration
BENCHMARKS = ["stereovision2", "diffeq2", "bgm", "blob_merge"]
SEEDS = [1, 2, 3, 4, 5]
DELTA = 20
LAMBDA = 0.02

VPR_PATH = "/home/os717/vtr-verilog-to-routing/vpr/vpr"
ARCH = "/home/os717/vtr-verilog-to-routing/vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BLIF_DIR = "/home/os717/vtr-verilog-to-routing/temp_synth"
OUTPUT_DIR = "verify_huber_robustness"

def run_vpr(bench, seed, mode):
    """
    mode: 'baseline' or 'huber'
    """
    run_id = f"{mode}_s{seed}"
    run_dir = Path(OUTPUT_DIR) / bench / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    log_path = run_dir / "vpr_stdout.log"
    
    cmd = [
        VPR_PATH, ARCH, f"{BLIF_DIR}/{bench}.abc.blif",
        "--route_chan_width", "300",
        "--seed", str(seed),
        "--disp", "off"
    ]
    
    if mode == 'huber':
        cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_mode", "4",
            "--prob_timing_beta", "1.0",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", str(LAMBDA),
            "--prob_dist_func", "huber",
            "--prob_huber_delta", str(DELTA),
            "--prob_timing_strategy", "off"
        ]
    else:
        cmd += ["--prob_timing_enable", "off"]

    try:
        with open(log_path, "w") as f:
            start_time = time.time()
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(run_dir), check=True)
            runtime = time.time() - start_time
        
        # Parse CPD
        with open(log_path, "r") as f:
            content = f.read()
            match = re.search(r"Final critical path delay \(least slack\):\s+([0-9.]+)\s+ns", content)
            if match:
                cpd = float(match.group(1))
                return bench, seed, mode, cpd, runtime
            else:
                return bench, seed, mode, "PARSE_FAIL", 0.0
    except subprocess.CalledProcessError:
        return bench, seed, mode, "VPR_FAIL", 0.0

def main():
    print(f"Starting Huber Robustness Validation (Delta={DELTA}, Lambda={LAMBDA})...")
    results = []
    
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    
    tasks = []
    # Using 12 workers to balance speed and system load
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for bench in BENCHMARKS:
            for seed in SEEDS:
                tasks.append(executor.submit(run_vpr, bench, seed, 'baseline'))
                tasks.append(executor.submit(run_vpr, bench, seed, 'huber'))
        
        total_tasks = len(tasks)
        completed = 0
        for future in concurrent.futures.as_completed(tasks):
            bench, seed, mode, res, runtime = future.result()
            results.append({
                "benchmark": bench,
                "seed": seed,
                "mode": mode,
                "cpd": res,
                "runtime": runtime
            })
            completed += 1
            print(f"[{completed}/{total_tasks}] {bench:<15} Seed {seed} {mode:<10} | CPD: {res}")
            
    # Process Results
    df = pd.DataFrame(results)
    df.to_csv("huber_robustness_results.csv", index=False)
    
    # Calculate Gains
    summary = []
    for bench in BENCHMARKS:
        bench_df = df[df['benchmark'] == bench]
        for seed in SEEDS:
            b_val = bench_df[(bench_df['seed'] == seed) & (bench_df['mode'] == 'baseline')]['cpd'].values[0]
            h_val = bench_df[(bench_df['seed'] == seed) & (bench_df['mode'] == 'huber')]['cpd'].values[0]
            
            if isinstance(b_val, float) and isinstance(h_val, float):
                gain = (b_val - h_val) / b_val * 100
                summary.append({
                    "benchmark": bench,
                    "seed": seed,
                    "baseline": b_val,
                    "huber": h_val,
                    "gain (%)": gain
                })
    
    summary_df = pd.DataFrame(summary)
    print("\n=== ROBUSTNESS SUMMARY ===")
    print(summary_df.to_string(index=False))
    
    # Per-benchmark averages
    print("\n=== PER-BENCHMARK AVERAGE GAIN ===")
    avg_gains = summary_df.groupby('benchmark')['gain (%)'].mean()
    print(avg_gains)
    
    # Geomean of gains? Or just mean of gains.
    print(f"\nOverall Average Gain: {avg_gains.mean():.2f}%")

if __name__ == "__main__":
    main()
