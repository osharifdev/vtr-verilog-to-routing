import subprocess
import re
import os
import concurrent.futures
from pathlib import Path
import time

# Experiment Configurations
EXP_QUAD_ROBUST = {
    "name": "quad_robust_sweep",
    "benchmarks": ["stereovision2", "diffeq2", "bgm", "blob_merge"],
    "func": "quadratic",
    "lambdas": [0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.1],
    "thresholds": [0] # Ignored for quadratic
}

VPR_PATH = "/home/os717/vtr-verilog-to-routing/vpr/vpr"
ARCH = "/home/os717/vtr-verilog-to-routing/vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BLIF_DIR = "/home/os717/vtr-verilog-to-routing/temp_synth"
OUTPUT_DIR = "sweep_robustness"

# Baseline CPDs (approximate, from Phase 0/1) for normalization
BASELINES = {
    "stereovision2": 18.62,
    "diffeq2": 18.35,
    "bgm": 18.52,
    "blob_merge": 10.07
}

def run_vpr(bench, l):
    run_id = f"{bench}_l{l}"
    run_dir = Path(OUTPUT_DIR) / bench / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    log_path = run_dir / "vpr_stdout.log"
    
    cmd = [
        VPR_PATH, ARCH, f"{BLIF_DIR}/{bench}.abc.blif",
        "--route_chan_width", "300",
        "--seed", "1",
        "--disp", "off",
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4",
        "--prob_timing_beta", "1.0",
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize",
        "--prob_inject_lambda", str(l),
        "--prob_dist_func", "quadratic",
        "--prob_timing_strategy", "off"
    ]
    
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
                return bench, l, cpd, runtime
            else:
                return bench, l, "PARSE_FAIL", 0.0
    except subprocess.CalledProcessError:
        return bench, l, "VPR_FAIL", 0.0

def run_experiment(exp_config):
    print(f"\n--- Starting Robustness Experiment ---")
    results = {}
    
    tasks = []
    # Limit concurrency to avoid system overload (25 runs total)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for b in exp_config["benchmarks"]:
            for l in exp_config["lambdas"]:
                tasks.append(executor.submit(run_vpr, b, l))
        
        for future in concurrent.futures.as_completed(tasks):
            bench, l, cpd, runtime = future.result()
            
            # Calculate Gain
            gain_str = "N/A"
            if isinstance(cpd, float):
                baseline = BASELINES.get(bench, cpd)
                gain = (baseline - cpd) / baseline * 100.0
                gain_str = f"{gain:+.2f}%"
                
            print(f"DONE {bench:<15} L={l:<6} | CPD: {cpd} ({gain_str}) (Run: {runtime:.1f}s)")
            
            if bench not in results: results[bench] = {}
            results[bench][l] = (cpd, gain_str)
            
    return results

def main():
    print("Running Quadratic Robustness Sweep...")
    run_experiment(EXP_QUAD_ROBUST)

if __name__ == "__main__":
    main()
