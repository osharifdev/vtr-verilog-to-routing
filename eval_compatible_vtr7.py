#!/usr/bin/env python3
import os
import subprocess
import csv
import math
import numpy as np

SEEDS = list(range(1, 26))
CORES_LIST = [8, 12, 16, 32]
# Only VTR 7.0 benchmarks that are pure LUTs/FFs (No DSPs/BRAMs)
COMPATIBLE_BENCHMARKS = [
    "vtr_flow/benchmarks/blif/stereovision0.blif",
    "vtr_flow/benchmarks/blif/boundtop.blif",
    "vtr_flow/benchmarks/blif/raygentop.blif",
    "vtr_flow/benchmarks/blif/sha.blif"
]

def main():
    report_lines = [
        "# VTR 7.0 Compatible Benchmarks - Single-Wave Budgeted Portfolio Evaluation",
        "- **Metric**: CPD Mean Reduction (% Difference = (Baseline - Budgeted) / Baseline * 100)",
        "- **Metric**: Latency Overhead (% Difference = (Budgeted - Baseline) / Baseline * 100)",
        ""
    ]
    
    for blif_path in COMPATIBLE_BENCHMARKS:
        bench_name = os.path.basename(blif_path).replace(".blif", "")
        print(f"\n============================================")
        print(f"Executing Sweep for Benchmark: {bench_name}")
        print(f"============================================")
        
        bench_lines = [f"## Benchmark: {bench_name}"]
        
        for cores in CORES_LIST:
            print(f"-- Configuration: {cores} Cores (Single Wave) --")
            results = []
            ran_any = False
            
            for seed in SEEDS:
                out_dir = f"eval_{bench_name}_{cores}c_seed{seed}"
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
                    try:
                        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except subprocess.CalledProcessError:
                        pass
                
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
            
            if not ran_any:
                print("  [INFO] Values already exist.")
            print(f"  Geometric Mean of % Differences: {geomean_pct_gain:.3f}%")
            print(f"  % Difference (Mean CPDs): {mean_pct_diff_cpd:.3f}%")
            print(f"  % Difference (Mean Latency Overhead): {mean_pct_diff_lat:.2f}%")
            print(f"  Done. Mean CPD Diff: {mean_pct_diff_cpd:.3f}%, Worst Seed Gain: {worst_gain:.3f}%, Lat Overhead: {mean_pct_diff_lat:.3f}%")
            
            bench_lines.extend([
                f"### {cores} Cores (Budget: {cores} runs)",
                "#### Critical Path Delay (CPD)",
                f"- **Baseline CPD**: Mean = {mean_base_cpd:.4f} ns, StdDev = {std_base_cpd:.4f}",
                f"- **Budgeted CPD**: Mean = {mean_best_cpd:.4f} ns, StdDev = {std_best_cpd:.4f}",
                f"- **% Difference (Mean Baseline vs Mean Budgeted)**: **{mean_pct_diff_cpd:.3f}%**",
                f"- **Geometric Mean of % Differences**: **{geomean_pct_gain:.3f}%**",
                f"- **Worst Case Percentage Gain per Seed**: **{worst_gain:.3f}%**",
                "#### Wallclock Time (Latency)",
                f"- **Baseline Latency**: Mean = {mean_base_lat:.2f} s, StdDev = {std_base_lat:.2f}",
                f"- **Budgeted Latency**: Mean = {mean_best_lat:.2f} s, StdDev = {std_best_lat:.2f}",
                f"- **% Difference (Mean Latency Overhead)**: **{mean_pct_diff_lat:.2f}%**",
                ""
            ])
            
        report_lines.extend(bench_lines)

    report_content = "\n".join(report_lines)
    with open("eval_compatible_vtr7_report.md", "w") as f:
        f.write(report_content)
        
    print("\n--- DONE ---")

if __name__ == "__main__":
    main()
