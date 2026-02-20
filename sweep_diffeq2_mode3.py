
import subprocess
import os
import re
import concurrent.futures

import shutil

VPR = os.path.abspath("./build/vpr/vpr")
ARCH = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
CIRCUIT_SRC = os.path.abspath("run_vtr7_diffeq2_baseline/diffeq2.pre-vpr.blif")

def run_vpr(seed, mode_prob=False):
    dirname = f"sweep_diffeq2_seed{seed}_{'prob' if mode_prob else 'base'}"
    os.makedirs(dirname, exist_ok=True)
    
    # Copy BLIF to ensure isolation
    local_blif = os.path.join(dirname, "diffeq2.blif")
    shutil.copy2(CIRCUIT_SRC, local_blif)
    
    cmd = [
        VPR, ARCH, "diffeq2.blif",
        "--route_chan_width", "100",
        "--seed", str(seed),
        "--disp", "off",
        "--pack", "--place", "--route", "--analysis",
        "--place_algorithm", "criticality_timing"
    ]
    
    if mode_prob:
        cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_alpha", "0.1",
            "--prob_timing_alpha_corr", "0.8",
            "--prob_timing_mc_samples", "1000",
            "--prob_timing_risk_z", "1.645",
            "--prob_timing_mode", "3",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", "0.7",
            "--prob_timing_strategy", "crit_boost"
        ]
        
    log_file = "vpr_stdout.log"
    with open(os.path.join(dirname, log_file), "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=True, cwd=dirname)
        
    # Parse CPD
    cpd = 0.0
    with open(os.path.join(dirname, log_file), "r") as f:
        for line in f:
            m = re.search(r"Final geomean non-virtual intra-domain period: ([\d.]+) ns", line)
            if m:
                cpd = float(m.group(1))
    return cpd

def test_seed(seed):
    base_cpd = run_vpr(seed, mode_prob=False)
    prob_cpd = run_vpr(seed, mode_prob=True)
    gain = (base_cpd - prob_cpd) / base_cpd * 100
    print(f"Seed {seed:2}: Base={base_cpd:7.3f} ns, Prob={prob_cpd:7.3f} ns, Gain={gain:6.2f}%")
    return seed, gain

seeds = range(1, 11)
print(f"Starting seed sweep on diffeq2 (seeds 1-10)...")
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    results = list(executor.map(test_seed, seeds))

print("\n--- Summary ---")
for seed, gain in results:
    match = "**** MATCH ****" if gain >= 7.0 else ""
    print(f"Seed {seed:2}: Gain={gain:6.2f}% {match}")
