#!/usr/bin/env python3
import os
import subprocess
import csv
import math
import numpy as np

SEEDS = list(range(1, 11))
CORES = 8
WAVES = 1

def main():
    results = []
    
    for seed in SEEDS:
        print(f"Running Seed {seed}...")
        out_dir = f"eval_8_1_seed{seed}"
        cmd = [
            "python3", "run_alu4_budgeted_portfolio.py",
            "--seed", str(seed),
            "--cores", str(CORES),
            "--waves", str(WAVES),
            "--out_dir", out_dir
        ]
        subprocess.run(cmd, check=True)
        
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
            # Gain calculation: (Baseline - Best) / Baseline * 100  (positive is good)
            speedup_ratio = baseline_cpd / best_cpd
            pct_gain = (speedup_ratio - 1) * 100
            
            # Sanity check: since baseline is in the pool, best_cpd <= baseline_cpd
            # Therefore pct_gain should be >= 0 always.
            results.append({
                "seed": seed,
                "baseline_cpd": baseline_cpd,
                "best_cpd": best_cpd,
                "speedup_ratio": speedup_ratio,
                "pct_gain": pct_gain
            })
            print(f"  Baseline: {baseline_cpd:.4f}  Best: {best_cpd:.4f}  Gain: {pct_gain:.2f}%")
        else:
            print(f"  Failed to parse results for seed {seed}")

    # Generate Report
    baseline_vals = [r['baseline_cpd'] for r in results]
    best_vals = [r['best_cpd'] for r in results]
    ratios = [r['speedup_ratio'] for r in results]
    
    geomean_ratio = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
    geomean_pct = (geomean_ratio - 1) * 100
    
    mean_base = np.mean(baseline_vals)
    std_base = np.std(baseline_vals, ddof=1) if len(baseline_vals) > 1 else 0
    
    mean_best = np.mean(best_vals)
    std_best = np.std(best_vals, ddof=1) if len(best_vals) > 1 else 0
    
    worst_gain = min(r['pct_gain'] for r in results)
    
    report_lines = [
        "# 10-Seed Budgeted Portfolio Evaluation",
        f"- **Configuration**: {CORES} cores, {WAVES} wave (Budget: {CORES*WAVES} runs per seed)",
        "- **Benchmark**: `alu4`",
        "",
        "## Overall Statistics",
        f"- **Baseline CPD**: Mean = {mean_base:.4f} ns, StdDev = {std_base:.4f}",
        f"- **Budgeted CPD**: Mean = {mean_best:.4f} ns, StdDev = {std_best:.4f}",
        f"- **Geometric Mean Speedup Ratio (Baseline/Budgeted)**: {geomean_ratio:.5f}",
        f"- **Geometric Mean Percentage Gain**: **{geomean_pct:.3f}%**",
        f"- **Worst Case Percentage Gain**: **{worst_gain:.3f}%** (Target: >= 0%)",
        "",
        "## Per-Seed Breakdown",
        "| Seed | Baseline CPD (ns) | Budgeted Best (ns) | % Gain |",
        "| :---: | :---: | :---: | :---: |"
    ]
    
    for r in results:
        report_lines.append(f"| {r['seed']} | {r['baseline_cpd']:.4f} | {r['best_cpd']:.4f} | {r['pct_gain']:.2f}% |")

    report_content = "\n".join(report_lines)
    
    with open("budget_10seed_report.md", "w") as f:
        f.write(report_content)
        
    print("\n--- DONE ---")
    print(report_content)

if __name__ == "__main__":
    main()
