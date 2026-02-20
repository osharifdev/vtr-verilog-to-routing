import subprocess
import re
import os
import concurrent.futures
from pathlib import Path
import time

# Experiment Configurations
EXP_QUAD = {
    "name": "bgm_quad_sweep",
    "benchmarks": ["bgm"],
    "func": "quadratic",
    "lambdas": [0.005, 0.01, 0.015, 0.02, 0.05],
    "thresholds": [0] # Ignored
}

VPR_PATH = "/home/os717/vtr-verilog-to-routing/vpr/vpr"
ARCH = "/home/os717/vtr-verilog-to-routing/vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BLIF_DIR = "/home/os717/vtr-verilog-to-routing/temp_synth"
OUTPUT_DIR = "sweep_geometric"

def run_vpr(bench, func, l, thresh):
    run_id = f"{func}_l{l}_th{thresh}"
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
        "--prob_dist_func", func,
        "--prob_dist_threshold", str(thresh),
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
                return bench, func, l, thresh, cpd, runtime
            else:
                return bench, func, l, thresh, "PARSE_FAIL", 0.0
    except subprocess.CalledProcessError:
        return bench, func, l, thresh, "VPR_FAIL", 0.0

def run_experiment(exp_config):
    print(f"\n--- Starting Experiment: {exp_config['name']} ---")
    results = {}
    
    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        for b in exp_config["benchmarks"]:
            for l in exp_config["lambdas"]:
                for th in exp_config["thresholds"]:
                    tasks.append(executor.submit(run_vpr, b, exp_config["func"], l, th))
        
        for future in concurrent.futures.as_completed(tasks):
            bench, func, l, thresh, res, runtime = future.result()
            key = (bench, func, l, thresh)
            results[key] = res
            print(f"DONE {bench:<15} {func:<10} L={l:<6} TH={thresh:<3} | CPD: {res} (Run: {runtime:.1f}s)")
            
    return results

def main():
    print("Running BGM Quadratic Sweep...")
    run_experiment(EXP_QUAD)

if __name__ == "__main__":
    main()
