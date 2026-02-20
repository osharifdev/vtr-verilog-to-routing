import subprocess
import re
import os
import concurrent.futures
from pathlib import Path
import time
import statistics

# Configuration
EXP_NAME = "multiseed_verify"
BENCHMARKS = ["stereovision2", "diffeq2", "bgm", "blob_merge"]
SEEDS = [1, 2, 3, 4, 5]
LAMBDA_TARGET = 0.02

VPR_PATH = "/home/os717/vtr-verilog-to-routing/vpr/vpr"
ARCH = "/home/os717/vtr-verilog-to-routing/vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BLIF_DIR = "/home/os717/vtr-verilog-to-routing/temp_synth"
OUTPUT_DIR = "verify_multiseed"

def run_vpr(bench, seed, config_type):
    run_id = f"{bench}_s{seed}_{config_type}"
    run_dir = Path(OUTPUT_DIR) / bench / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    log_path = run_dir / "vpr_stdout.log"
    
    cmd = [
        VPR_PATH, ARCH, f"{BLIF_DIR}/{bench}.abc.blif",
        "--route_chan_width", "300",
        "--seed", str(seed),
        "--disp", "off",
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4",
        "--prob_timing_beta", "1.0",
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize",
        "--prob_timing_strategy", "off"
    ]
    
    if config_type == "baseline":
        # Baseline: Effectively disabled injection (Lambda=0)
        # Note: Mode 4 with Lambda=0 is mathematically equivalent to Mode 0/Default
        # But running in Mode 4 ensures code path consistency.
        cmd += ["--prob_inject_lambda", "0.0"]
        cmd += ["--prob_dist_func", "linear"] # Doesn't matter if lambda=0
    else:
        # Experimental: Quadratic L=0.02
        cmd += ["--prob_inject_lambda", str(LAMBDA_TARGET)]
        cmd += ["--prob_dist_func", "quadratic"]
    
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
                return bench, seed, config_type, cpd
            else:
                return bench, seed, config_type, None
    except subprocess.CalledProcessError:
        return bench, seed, config_type, None

def run_experiment():
    print(f"\n--- Starting Multi-Seed Verification (L={LAMBDA_TARGET}) ---")
    results = {b: {"baseline": {}, "experiment": {}} for b in BENCHMARKS}
    
    tasks = []
    # 4 benchmarks * 5 seeds * 2 configs = 40 runs
    # Limit parallelism
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as executor:
        for b in BENCHMARKS:
            for s in SEEDS:
                # Submit Baseline
                tasks.append(executor.submit(run_vpr, b, s, "baseline"))
                # Submit Experiment
                tasks.append(executor.submit(run_vpr, b, s, "experiment"))
        
        for future in concurrent.futures.as_completed(tasks):
            bench, seed, c_type, cpd = future.result()
            
            if cpd:
                results[bench][c_type][seed] = cpd
                status = f"{cpd} ns"
            else:
                status = "FAIL"
                
            print(f"DONE {bench:<15} Seed={seed} [{c_type:<10}] | CPD: {status}")

    # Analysis
    print("\n--- Summary Analysis ---")
    print(f"{'Benchmark':<15} | {'Base (Avg)':<10} | {'Quad (Avg)':<10} | {'Avg Gain':<10} | {'Min Gain':<10} | {'Max Gain':<10}")
    print("-" * 80)
    
    for b in BENCHMARKS:
        base_cpds = results[b]["baseline"]
        exp_cpds = results[b]["experiment"]
        
        if not base_cpds or not exp_cpds:
            print(f"{b:<15} | MISSING DATA")
            continue
            
        gains = []
        valid_seeds = set(base_cpds.keys()) & set(exp_cpds.keys())
        
        for s in valid_seeds:
            base = base_cpds[s]
            exp = exp_cpds[s]
            gain = (base - exp) / base * 100.0
            gains.append(gain)
            
        if gains:
            avg_base = statistics.mean([base_cpds[s] for s in valid_seeds])
            avg_exp = statistics.mean([exp_cpds[s] for s in valid_seeds])
            avg_gain = statistics.mean(gains)
            min_gain = min(gains)
            max_gain = max(gains)
            
            print(f"{b:<15} | {avg_base:<10.2f} | {avg_exp:<10.2f} | {avg_gain:<+10.2f}% | {min_gain:<+10.2f}% | {max_gain:<+10.2f}%")

if __name__ == "__main__":
    run_experiment()
