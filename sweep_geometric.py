import subprocess
import re
import os
import concurrent.futures
from pathlib import Path
import time

# Experiment Configurations
EXP_STEP = {
    "name": "step_sweep",
    "benchmarks": ["blob_merge", "stereovision2"],
    "func": "step",
    "lambdas": [0.1, 0.15, 0.2],
    "thresholds": [5, 10, 15, 20]
}

EXP_QUAD = {
    "name": "quad_sweep",
    "benchmarks": ["stereovision2", "diffeq2", "blob_merge"],
    "func": "quadratic",
    "lambdas": [0.005, 0.01, 0.015, 0.02, 0.05],
    "thresholds": [0] # Ignored
}

VPR_PATH = "/home/os717/vtr-verilog-to-routing/vpr/vpr"
# Using the same architecture as before
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
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
    print("Running Geometric Sweeps...")
    
    # Run Step Sweep
    step_results = run_experiment(EXP_STEP)
    
    # Run Quadratic Sweep
    quad_results = run_experiment(EXP_QUAD)
    
    # Print Summary
    print("\n=== SUMMARY ===")
    all_results = {**step_results, **quad_results}
    
    # Sort keys for nice printing
    # Group by Benchmark, then Func
    
    organized = {}
    for key, val in all_results.items():
        bench, func, l, thresh = key
        if bench not in organized: organized[bench] = []
        organized[bench].append((func, l, thresh, val))
        
    for bench in organized:
        print(f"\nBenchmark: {bench}")
        print(f"{'Func':<10} {'Lambda':<8} {'Thresh':<8} {'CPD (ns)':<10}")
        print("-" * 40)
        
        # Sort by CPD (ascending) to find best
        items = organized[bench]
        items.sort(key=lambda x: x[3] if isinstance(x[3], float) else 9999.9)
        
        for func, l, thresh, val in items:
            print(f"{func:<10} {l:<8} {thresh:<8} {val:<10}")

if __name__ == "__main__":
    main()
