#!/usr/bin/env python3
import os
import subprocess
import csv
import math
import numpy as np

SEEDS = list(range(1, 26))
CONFIGS = [
    (8, 1),
    (8, 2),
    (8, 3),
    (12, 1),
    (12, 2),
    (16, 1),
    (16, 2)
]

def main():
    report_lines = [
        "# 25-Seed Budgeted Portfolio Evaluation",
        "- **Benchmark**: `alu4`",
        "- **Metric**: CPD Mean Reduction (% Difference = (Baseline Mean - Budgeted Mean) / Baseline Mean * 100)",
        ""
    ]
    
    for cores, waves in CONFIGS:
        print(f"=== Running Configuration: {cores} Cores, {waves} Waves ===")
        results = []
        
        for seed in SEEDS:
            out_dir = f"eval_{cores}_{waves}_seed{seed}"
            cmd = [
                "python3", "run_alu4_budgeted_portfolio.py",
                "--seed", str(seed),
                "--cores", str(cores),
                "--waves", str(waves),
                "--out_dir", out_dir
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Parse results
            csv_path = os.path.join(out_dir, "stage2_full_runs.csv")
            baseline_cpd = None
            best_cpd = None
            
            with open(csv_path, 'r') as f:
                reader = csv.DictReader(f)
                runs = list(reader)
                
                for r in runs:
                    if r['status'] == 'OK' and r['cpd_ns']:
                        cpd = float(r['cpd_ns'])
                        if r['type'] == 'baseline':
                            baseline_cpd = cpd
                        
                        if best_cpd is None or cpd < best_cpd:
                            best_cpd = cpd
                            
            if baseline_cpd is not None and best_cpd is not None:
                speedup_ratio = baseline_cpd / best_cpd
                pct_gain = (speedup_ratio - 1) * 100
                
                results.append({
                    "seed": seed,
                    "baseline_cpd": baseline_cpd,
                    "best_cpd": best_cpd,
                    "pct_gain": pct_gain
                })
            else:
                print(f"  Warning: Failed to parse results for {cores}c {waves}w seed {seed}")

        if not results:
            continue
            
        baseline_vals = [r['baseline_cpd'] for r in results]
        best_vals = [r['best_cpd'] for r in results]
        
        mean_base = np.mean(baseline_vals)
        std_base = np.std(baseline_vals, ddof=1) if len(baseline_vals) > 1 else 0
        
        mean_best = np.mean(best_vals)
        std_best = np.std(best_vals, ddof=1) if len(best_vals) > 1 else 0
        
        worst_gain = min(r['pct_gain'] for r in results)
        
        # New Percentage difference statistic
        mean_reduction_pct = ((mean_base - mean_best) / mean_base) * 100

        print(f"  Done. Reduction: {mean_reduction_pct:.3f}%  Worst-case seed gain: {worst_gain:.3f}%")
        
        report_lines.extend([
            f"## {cores} Cores, {waves} Wave(s) (Budget: {cores*waves} runs)",
            f"- **Baseline CPD**: Mean = {mean_base:.4f} ns, StdDev = {std_base:.4f}",
            f"- **Budgeted CPD**: Mean = {mean_best:.4f} ns, StdDev = {std_best:.4f}",
            f"- **CPD Mean Reduction**: **{mean_reduction_pct:.3f}%**",
            f"- **Worst Case Percentage Gain per Seed**: **{worst_gain:.3f}%** (Target: >= 0%)",
            ""
        ])

    report_content = "\n".join(report_lines)
    
    with open("budget_25seed_all_report.md", "w") as f:
        f.write(report_content)
        
    print("\n--- DONE ---")
    print(report_content)

if __name__ == "__main__":
    main()
