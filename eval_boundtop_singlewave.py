#!/usr/bin/env python3
import os
import subprocess
import csv
import math
import numpy as np

SEEDS = list(range(1, 26))
CORES_LIST = [8, 12, 16, 32]
BLIF_PATH = "vtr7_6lut_mapped/boundtop.blif"

def main():
    bench_name = os.path.basename(BLIF_PATH).replace(".blif", "")
    report_lines = [
        f"# Single-Wave Budgeted Portfolio Evaluation - {bench_name}",
        f"- **Benchmark**: {BLIF_PATH} (Smallest Pure-LUT/FF VTR7 Benchmark)",
        "- **Metric**: CPD Mean Reduction (% Difference = (Baseline - Budgeted) / Baseline * 100)",
        "- **Metric**: Latency Overhead (% Difference = (Budgeted - Baseline) / Baseline * 100)",
        ""
    ]
    
    for cores in CORES_LIST:
        print(f"=== Running Configuration: {cores} Cores (Single Wave) ===")
        results = []
        
        for seed in SEEDS:
            out_dir = f"eval_{bench_name}_{cores}c_seed{seed}"
            cmd = [
                "python3", "run_budgeted_portfolio_singlewave.py",
                "--blif", BLIF_PATH,
                "--seed", str(seed),
                "--cores", str(cores),
                "--out_dir", out_dir
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            csv_path = os.path.join(out_dir, "stage2_full_runs.csv")
            baseline_cpd = None
            baseline_lat = None
            best_cpd = None
            budget_lat = 0.0
            
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
            else:
                print(f"  Warning: Failed to parse results for {cores}c seed {seed}")

        if not results:
            continue
            
        mean_base_cpd = np.mean([r['baseline_cpd'] for r in results])
        std_base_cpd = np.std([r['baseline_cpd'] for r in results], ddof=1)
        mean_best_cpd = np.mean([r['best_cpd'] for r in results])
        std_best_cpd = np.std([r['best_cpd'] for r in results], ddof=1)
        
        mean_base_lat = np.mean([r['baseline_lat'] for r in results])
        std_base_lat = np.std([r['baseline_lat'] for r in results], ddof=1)
        mean_best_lat = np.mean([r['budget_lat'] for r in results])
        std_best_lat = np.std([r['budget_lat'] for r in results], ddof=1)
        
        ratios = [r['baseline_cpd'] / r['best_cpd'] for r in results]
        geomean_ratio = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
        geomean_pct_gain = (geomean_ratio - 1) * 100
        
        mean_pct_diff_cpd = ((mean_base_cpd - mean_best_cpd) / mean_base_cpd) * 100
        mean_pct_diff_lat = ((mean_best_lat - mean_base_lat) / mean_base_lat) * 100
        worst_gain = min(r['pct_gain_cpd'] for r in results)
        
        print(f"  Done. CPD Diff: {mean_pct_diff_cpd:.3f}%  Lat Overhead: {mean_pct_diff_lat:.3f}%")
        
        report_lines.extend([
            f"## {cores} Cores (Budget: {cores} runs)",
            "### Critical Path Delay (CPD)",
            f"- **Baseline CPD**: Mean = {mean_base_cpd:.4f} ns, StdDev = {std_base_cpd:.4f}",
            f"- **Budgeted CPD**: Mean = {mean_best_cpd:.4f} ns, StdDev = {std_best_cpd:.4f}",
            f"- **% Difference (Mean Baseline vs Mean Budgeted)**: **{mean_pct_diff_cpd:.3f}%**",
            f"- **Geometric Mean of % Differences**: **{geomean_pct_gain:.3f}%**",
            f"- **Worst Case Percentage Gain per Seed**: **{worst_gain:.3f}%** (Target: >= 0%)",
            "### Wallclock Time (Latency)",
            f"- **Baseline Latency**: Mean = {mean_base_lat:.2f} s, StdDev = {std_base_lat:.2f}",
            f"- **Budgeted Latency**: Mean = {mean_best_lat:.2f} s, StdDev = {std_best_lat:.2f}",
            f"- **% Difference (Mean Latency Overhead)**: **{mean_pct_diff_lat:.2f}%**",
            ""
        ])

    report_content = "\n".join(report_lines)
    with open("singlewave_boundtop_report.md", "w") as f:
        f.write(report_content)
        
    print("\n--- DONE ---")

if __name__ == "__main__":
    main()
