import subprocess
import os
import re

VPR_EXEC = os.path.abspath("./build/vpr/vpr")
ARCH_FILE = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
CIRCUIT_FILE = os.path.abspath("run_vtr7_diffeq2_baseline/diffeq2.pre-vpr.blif")

def get_cpd(log_file):
    if not os.path.exists(log_file): return 0.0
    with open(log_file, "r") as f:
        for line in f:
            if "Final critical path delay" in line:
                return float(re.search(r"Final critical path delay.*: ([\d\.]+) ns", line).group(1))
    return 0.0

def run_vpr(seed, mode, tag):
    work_dir = f"cross_seed_test_{tag}"
    os.makedirs(work_dir, exist_ok=True)
    
    cmd = [
        VPR_EXEC, ARCH_FILE, CIRCUIT_FILE,
        "--route_chan_width", "100",
        "--seed", str(seed),
        "--disp", "off",
        "--pack", "--place", "--route", "--analysis",
        "--place_algorithm", "criticality_timing"
    ]
    
    if mode == "prob":
        cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_timing_mode", "3",
            "--prob_timing_strategy", "crit_boost",
            "--prob_timing_alpha", "0.1",
            "--prob_timing_alpha_corr", "0.8",
            "--prob_inject_lambda", "0.7"
        ]
        
    subprocess.run(cmd, cwd=work_dir, stdout=open(f"{work_dir}/vpr.log", "w"), stderr=subprocess.STDOUT)
    return get_cpd(f"{work_dir}/vpr.log")

print("Running Cross-Seed Test for diffeq2...")

# 1. Baseline Seed 1
base_s1 = run_vpr(1, "base", "base_s1")
print(f"Baseline Seed 1 CPD: {base_s1:.4f} ns")

# 2. Probabilistic Seed 13
prob_s13 = run_vpr(13, "prob", "prob_s13")
print(f"Probabilistic Seed 13 CPD: {prob_s13:.4f} ns")

# Calculate Gain
if base_s1 > 0:
    gain = (base_s1 - prob_s13) / base_s1 * 100
    print(f"Gain (Base S1 vs Prob S13): {gain:+.2f}%")
else:
    print("Baseline Seed 1 failed.")
