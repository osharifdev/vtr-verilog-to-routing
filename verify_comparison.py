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
OUTPUT_DIR = "verify_all_models"

def run_vpr(bench, seed, mode):
    """
    mode: 'baseline', 'quadratic', or 'huber'
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
    elif mode == 'quadratic':
        cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_mode", "4",
            "--prob_timing_beta", "1.0",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", str(LAMBDA),
            "--prob_dist_func", "quadratic",
            "--prob_timing_strategy", "off"
        ]
    else:
        # Baseline Mode
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
    print(f"Starting Comparative Robustness Validation (L={LAMBDA}, D={DELTA})...")
    results = []
    
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    
    tasks = []
    # Using 12 workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        for bench in BENCHMARKS:
            for seed in SEEDS:
                tasks.append(executor.submit(run_vpr, bench, seed, 'baseline'))
                tasks.append(executor.submit(run_vpr, bench, seed, 'quadratic'))
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
            print(f"[{completed}/{total_tasks}] {bench:<15} S{seed} {mode:<10} | CPD: {res}")
            
    # Process Results
    df = pd.DataFrame(results)
    df.to_csv("comparison_robustness_results.csv", index=False)
    
    # Calculate Results per benchmark/seed
    final_summary = []
    for bench in BENCHMARKS:
        bench_df = df[df['benchmark'] == bench]
        for seed in SEEDS:
            row = {"benchmark": bench, "seed": seed}
            b_val = bench_df[(bench_df['seed'] == seed) & (bench_df['mode'] == 'baseline')]['cpd'].values[0]
            q_val = bench_df[(bench_df['seed'] == seed) & (bench_df['mode'] == 'quadratic')]['cpd'].values[0]
            h_val = bench_df[(bench_df['seed'] == seed) & (bench_df['mode'] == 'huber')]['cpd'].values[0]
            
            row['baseline'] = b_val
            row['quadratic'] = q_val
            row['huber'] = h_val
            
            if isinstance(b_val, float):
                if isinstance(q_val, float):
                    row['gain_q (%)'] = (b_val - q_val) / b_val * 100
                if isinstance(h_val, float):
                    row['gain_h (%)'] = (b_val - h_val) / b_val * 100
                    
            final_summary.append(row)
            
    summary_df = pd.DataFrame(final_summary)
    print("\n=== FINAL COMPARISON SUMMARY ===")
    print(summary_df.to_string(index=False))
    
    # Per-benchmark averages
    print("\n=== PER-BENCHMARK AVERAGE GAIN ===")
    avg_gains = summary_df.groupby('benchmark')[['gain_q (%)', 'gain_h (%)']].mean()
    print(avg_gains)
    
    overall_q = avg_gains['gain_q (%)'].mean()
    overall_h = avg_gains['gain_h (%)'].mean()
    print(f"\nOverall Quadratic Average Gain: {overall_q:.2f}%")
    print(f"Overall Huber Average Gain:     {overall_h:.2f}%")

if __name__ == "__main__":
    main()
