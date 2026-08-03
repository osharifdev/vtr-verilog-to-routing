#!/usr/bin/env python3
import os
import sys
import subprocess
import csv
import math
import numpy as np

# We provide an argument to limit seeds for testing, default to 100.
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--num_seeds", type=int, default=100, help="Number of seeds to run")
args = parser.parse_args()

SEEDS = list(range(1, args.num_seeds + 1))
CORES_LIST = [8, 12, 16, 24, 32]

# Compatible benchmarks mapped to their domain
BENCHMARKS = {
    "stereovision0": "vtr7_6lut_mapped/stereovision0.blif",
    "boundtop": "vtr7_6lut_mapped/boundtop.blif",
    "raygentop": "vtr7_6lut_mapped/raygentop.blif",
    "sha": "vtr7_6lut_mapped/sha.blif"
}

ALL_VTR7 = [
    "bgm", "blob_merge", "boundtop", "ch_intrinsics", "diffeq1", "diffeq2", 
    "LU8PEEng", "LU32PEEng", "mcml", "mkDelayWorker32B", "mkPktMerge", 
    "mkSMAdapter4B", "or1200", "raygentop", "sha", "stereovision0", 
    "stereovision1", "stereovision2", "stereovision3"
]

def create_folders():
    base_dir = "VTR_Benchmarks"
    os.makedirs(base_dir, exist_ok=True)
    for b in ALL_VTR7:
        os.makedirs(os.path.join(base_dir, b), exist_ok=True)
        
def progress_bar(iteration, total, length=40):
    percent = 100 * (iteration / float(total))
    filled = int(length * iteration // total)
    bar = '=' * filled + '-' * (length - filled)
    sys.stdout.write(f'\r  Progress: |{bar}| {percent:.1f}% ({iteration}/{total} seeds)')
    sys.stdout.flush()
    if iteration == total:
        print()

def main():
    create_folders()
    
    for bench_name, blif_path in BENCHMARKS.items():
        print(f"\n============================================")
        print(f"Executing Sweep for Benchmark: {bench_name}")
        print(f"============================================")
        
        bench_dir = os.path.join("VTR_Benchmarks", bench_name)
        report_lines = [
            f"# Single-Wave Budgeted Portfolio Evaluation - {bench_name}",
            f"- **Benchmark**: {blif_path}",
            f"- **Seeds Run**: {args.num_seeds}",
            "- **Metric**: CPD Mean Reduction (% Difference = (Baseline - Budgeted) / Baseline * 100)",
            "- **Metric**: Latency Overhead (% Difference = (Budgeted - Baseline) / Baseline * 100)",
            ""
        ]
        
        for cores in CORES_LIST:
            print(f"\n-- Configuration: {cores} Cores --")
            results = []
            ran_any = False
            
            for i, seed in enumerate(SEEDS):
                out_dir = os.path.join(bench_dir, f"eval_{cores}c_seed{seed}")
                csv_path = os.path.join(out_dir, "stage2_full_runs.csv")
                if not os.path.exists(csv_path):
                    ran_any = True
                    cmd = [
                        "python3", "run_budgeted_portfolio_singlewave.py",
                        "--blif", blif_path,
                        "--seed", str(seed),
                        "--cores", str(cores),
                        "--out_dir", out_dir
                    ]
                    
                    # Suppress output so it doesn't break the progress bar
                    try:
                        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except subprocess.CalledProcessError:
                        pass
                
                # Parse output
                csv_path = os.path.join(out_dir, "stage2_full_runs.csv")
                baseline_cpd = None
                baseline_lat = None
                best_cpd = None
                budget_lat = 0.0
                
                if os.path.exists(csv_path):
                    with open(csv_path, 'r') as f:
                        reader = csv.DictReader(f)
                        runs = list(reader)
                        for r in runs:
                            if r['status'] == 'OK' and r['cpd_ns']:
                                cpd = float(r['cpd_ns'])
                                lat = float(r['runtime_s'])
                                if r['type'] == 'baseline':
                                    baseline_cpd = cpd
                                    baseline_lat = lat
                                if best_cpd is None or cpd < best_cpd:
                                    best_cpd = cpd
                            if r['runtime_s']:
                                lat = float(r['runtime_s'])
                                if lat > budget_lat:
                                    budget_lat = lat
                                    
                if baseline_cpd is not None and best_cpd is not None:
                    pct_gain_cpd = (baseline_cpd - best_cpd) / baseline_cpd * 100
                    pct_overhead_lat = (budget_lat - baseline_lat) / baseline_lat * 100 if baseline_lat else 0
                    results.append({
                        "seed": seed,
                        "baseline_cpd": baseline_cpd,
                        "best_cpd": best_cpd,
                        "pct_gain_cpd": pct_gain_cpd,
                        "baseline_lat": baseline_lat,
                        "budget_lat": budget_lat,
                        "pct_overhead_lat": pct_overhead_lat
                    })
                
                progress_bar(i + 1, len(SEEDS))

            if not results:
                print("  [ERROR] No valid results produced.")
                continue
                
            mean_base_cpd = np.mean([r['baseline_cpd'] for r in results])
            mean_best_cpd = np.mean([r['best_cpd'] for r in results])
            mean_base_lat = np.mean([r['baseline_lat'] for r in results])
            mean_best_lat = np.mean([r['budget_lat'] for r in results])
            
            ratios = [r['baseline_cpd'] / r['best_cpd'] for r in results]
            geomean_ratio = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
            geomean_pct_gain = (geomean_ratio - 1) * 100
            mean_pct_diff_lat = ((mean_best_lat - mean_base_lat) / mean_base_lat) * 100 if mean_base_lat else 0
            mean_pct_diff_cpd = ((mean_base_cpd - mean_best_cpd) / mean_base_cpd) * 100 if mean_base_cpd else 0
            
            if not ran_any:
                print("  [INFO] Values already exist.")
            print(f"Geometric Mean of % Differences: {geomean_pct_gain:.3f}%")
            print(f"% Difference (Mean CPDs): {mean_pct_diff_cpd:.3f}%")
            print(f"% Difference (Mean Latency Overhead): {mean_pct_diff_lat:.2f}%")
            
            report_lines.extend([
                f"### {cores} Cores (Budget: {cores} runs)",
                f"- **Geometric Mean of % Differences**: **{geomean_pct_gain:.3f}%**",
                f"- **% Difference (Mean CPDs)**: **{mean_pct_diff_cpd:.3f}%**",
                f"- **% Difference (Mean Latency Overhead)**: **{mean_pct_diff_lat:.2f}%**",
                ""
            ])

        # Save cumulative report inside the benchmark directory
        report_path = os.path.join(bench_dir, "summary_report.md")
        with open(report_path, "w") as f:
            f.write("\n".join(report_lines))

if __name__ == "__main__":
    main()
