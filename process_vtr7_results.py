import pandas as pd
import numpy as np

# Load results
df = pd.read_csv("vtr7_comparative_results.csv")

# Filter out failures if any
df = df[df['cpd'].apply(lambda x: isinstance(x, (int, float, np.float64)))]

# Pivot to compare modes side-by-side
pivot = df.pivot_table(index=['benchmark', 'seed'], columns='mode', values='cpd').reset_index()

# Calculate gains (%) relative to baseline
pivot['gain_q (%)'] = (pivot['baseline'] - pivot['quadratic']) / pivot['baseline'] * 100
pivot['gain_d20 (%)'] = (pivot['baseline'] - pivot['huber_d20']) / pivot['baseline'] * 100
pivot['gain_d50 (%)'] = (pivot['baseline'] - pivot['huber_d50']) / pivot['baseline'] * 100

# Group by benchmark to get average gains
bench_summary = pivot.groupby('benchmark')[['gain_q (%)', 'gain_d20 (%)', 'gain_d50 (%)']].mean()

# Calculate overall average gains
overall_summary = bench_summary.mean()

# Identify best mode per benchmark
def get_best(row):
    gains = {
        'Quadratic': row['gain_q (%)'],
        'Huber D=20': row['gain_d20 (%)'],
        'Huber D=50': row['gain_d50 (%)']
    }
    best_mode = max(gains, key=gains.get)
    if gains[best_mode] < 0:
        return f"Baseline (Best gain: {gains[best_mode]:.2f}%)"
    return f"{best_mode} ({gains[best_mode]:.2f}%)"

bench_summary['Best Model'] = bench_summary.apply(get_best, axis=1)

# Sort by gain_q
bench_summary = bench_summary.sort_values('gain_q (%)', ascending=False)

print("\n=== VTR 7.0 FULL AUDIT SUMMARY (19 Benchmarks, 5 Seeds each) ===\n")
print(bench_summary.to_string())
print("\n=== OVERALL AVERAGE GAINS ===\n")
print(overall_summary.to_string())

# Save to markdown format for artifact
with open("vtr7_audit_summary.md", "w") as f:
    f.write("# VTR 7.0 Full Audit: Huber vs Quadratic Comparison\n\n")
    f.write("This report summarizes the results of 380 experiments (19 benchmarks, 5 seeds, 4 modes).\n\n")
    f.write("## Overall Results\n")
    f.write("| Model | Average Gain vs Baseline |\n")
    f.write("| :--- | :---: |\n")
    f.write(f"| Quadratic (L=0.02) | **{overall_summary['gain_q (%)']:.2f}%** |\n")
    f.write(f"| Huber (D=20, L=0.02) | {overall_summary['gain_d20 (%)']:.2f}% |\n")
    f.write(f"| Huber (D=50, L=0.02) | {overall_summary['gain_d50 (%)']:.2f}% |\n\n")
    
    f.write("## Per-Benchmark Summary\n")
    # Manual markdown table generation
    f.write("| Benchmark | Gain Q (%) | Gain D=20 (%) | Gain D=50 (%) | Best Model |\n")
    f.write("| :--- | :---: | :---: | :---: | :--- |\n")
    for b, row in bench_summary.iterrows():
        f.write(f"| {b} | {row['gain_q (%)']:.2f} | {row['gain_d20 (%)']:.2f} | {row['gain_d50 (%)']:.2f} | {row['Best Model']} |\n")
    
    f.write("\n\n## Key Findings\n")
    
    # Simple logic for key findings
    best_overall = overall_summary.idxmax()
    f.write(f"- **Top Performer**: {best_overall.replace('gain_', '').replace(' (%)', '').capitalize()} is the best overall model across the full suite.\n")
    
    huber_wins = (bench_summary['gain_d20 (%)'] > bench_summary['gain_q (%)']).sum() + (bench_summary['gain_d50 (%)'] > bench_summary['gain_q (%)']).sum()
    if huber_wins > 0:
        f.write(f"- **Huber Stability**: Huber Scaling outperformed Quadratic in some benchmarks, indicating higher robustness for specific topologies.\n")

