
import os
import subprocess
import re
import statistics
import sys

# Paths
VPR_EXEC = "./vpr/vpr"
ARCH_FILE = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BENCHMARK = "temp_synth/stereovision2.abc.blif"
ROUTE_CHAN_WIDTH = 300 # Increased for stereovision2

BETAS = [0.0]
SEEDS = [1]
ALPHA = 0.1

def run_vpr(beta, seed):
    # Mode 4 for Beta > 0, Mode 1 for Beta 0 (Baseline logic)
    mode = 4 if beta > 0 else 1
    
    cmd = [
        VPR_EXEC, ARCH_FILE, BENCHMARK,
        "--route_chan_width", str(ROUTE_CHAN_WIDTH),
        "--seed", str(seed),
        "--disp", "off",
        "--prob_timing_mode", str(mode),
        "--prob_timing_alpha", str(ALPHA),
        "--prob_timing_beta", str(beta),
        "--prob_timing_enable", "on"
    ]
    
    print(f"Running Beta={beta} Seed={seed}...")
    print("CMD:", " ".join(cmd))
    sys.stdout.flush()
    result = subprocess.run(cmd, capture_output=True, text=True) # Run without shell=True for list args
    
    if result.returncode != 0:
        print(f"FAILED Beta={beta} Seed={seed}")
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        return None

    # Parse CPD
    # Match: "Final critical path: 18.1555 ns"
    cpd_match = re.search(r"Final critical path: ([\d.]+) ns", result.stdout)
    if cpd_match:
        return float(cpd_match.group(1))
    
    # Fallback or error
    return None

def main():
    print(f"Starting Sweep: Benchmark={BENCHMARK}, CW={ROUTE_CHAN_WIDTH}")
    print(f"{'Beta':<10} | {'Seed':<5} | {'CPD (ns)':<10}")
    print("-" * 35)
    
    results = {}
    
    for beta in BETAS:
        results[beta] = []
        for seed in SEEDS:
            cpd = run_vpr(beta, seed)
            if cpd:
                print(f"{beta:<10.1f} | {seed:<5} | {cpd:<10.4f}")
                results[beta].append(cpd)
            else:
                print(f"{beta:<10.1f} | {seed:<5} | {'FAIL':<10}")

    print("\n--- Summary ---")
    print(f"{'Beta':<10} | {'Avg CPD':<10} | {'Gain (%)':<10}")
    print("-" * 40)
    
    baseline_avg = statistics.mean(results[0.0]) if 0.0 in results and results[0.0] else None
    
    for beta in BETAS:
        if beta not in results or not results[beta]:
            continue
            
        avg = statistics.mean(results[beta])
        gain = 0.0
        if baseline_avg:
            gain = ((baseline_avg - avg) / baseline_avg) * 100.0
            
        print(f"{beta:<10.1f} | {avg:<10.4f} | {gain:<10.2f}%")

if __name__ == "__main__":
    main()
