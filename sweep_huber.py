import subprocess
import re
import os
import concurrent.futures
from pathlib import Path
import time

# Huber Delta Sweep Configuration
EXPERIMENTS = [
    {"bench": "stereovision2", "seed": "5"},
    {"bench": "blob_merge", "seed": "2"}
]

DELTAS = [5, 10, 20, 50]
LAMBDA = 0.02
VPR_PATH = "/home/os717/vtr-verilog-to-routing/vpr/vpr"
ARCH = "/home/os717/vtr-verilog-to-routing/vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BLIF_DIR = "/home/os717/vtr-verilog-to-routing/temp_synth"
OUTPUT_DIR = "sweep_huber"

def run_vpr(bench, seed, delta=None):
    """
    Runs VPR. If delta is None, runs baseline (no injection).
    Otherwise runs Huber with the specified delta.
    """
    run_id = f"baseline_s{seed}" if delta is None else f"huber_d{delta}_s{seed}"
    run_dir = Path(OUTPUT_DIR) / bench / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    log_path = run_dir / "vpr_stdout.log"
    
    cmd = [
        VPR_PATH, ARCH, f"{BLIF_DIR}/{bench}.abc.blif",
        "--route_chan_width", "300",
        "--seed", seed,
        "--disp", "off"
    ]
    
    if delta is not None:
        cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_mode", "4",
            "--prob_timing_beta", "1.0",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", str(LAMBDA),
            "--prob_dist_func", "huber",
            "--prob_huber_delta", str(delta),
            "--prob_timing_strategy", "off"
        ]
    else:
        # Baseline Mode
        cmd += ["--prob_timing_enable", "off"]

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
                return bench, seed, delta, cpd, runtime
            else:
                return bench, seed, delta, "PARSE_FAIL", 0.0
    except subprocess.CalledProcessError:
        return bench, seed, delta, "VPR_FAIL", 0.0

def main():
    print(f"Starting Huber Delta Sweep (Lambda={LAMBDA})...")
    results = []
    
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    
    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for exp in EXPERIMENTS:
            # Run Baseline
            tasks.append(executor.submit(run_vpr, exp["bench"], exp["seed"], None))
            # Run Huber Deltas
            for d in DELTAS:
                tasks.append(executor.submit(run_vpr, exp["bench"], exp["seed"], d))
        
        for future in concurrent.futures.as_completed(tasks):
            bench, seed, delta, res, runtime = future.result()
            results.append((bench, seed, delta, res, runtime))
            label = f"D={delta}" if delta is not None else "Baseline"
            print(f"DONE {bench:<15} Seed {seed} {label:<10} | CPD: {res} (Run: {runtime:.1f}s)")
            
    # Print Summary Table
    print("\n=== HUBER SWEEP SUMMARY ===")
    for exp in EXPERIMENTS:
        bench = exp["bench"]
        seed = exp["seed"]
        print(f"\nBenchmark: {bench} (Seed {seed})")
        print(f"{'Mode':<15} {'CPD (ns)':<10} {'Gain (%)':<10}")
        print("-" * 35)
        
        # Find baseline
        baseline_cpd = next(r[3] for r in results if r[0] == bench and r[1] == seed and r[2] is None)
        
        # Sort deltas
        bench_res = [r for r in results if r[0] == bench and r[1] == seed]
        bench_res.sort(key=lambda x: (x[2] if x[2] is not None else -1))
        
        for b, s, d, cpd, rt in bench_res:
            label = f"Huber (D={d})" if d is not None else "Baseline"
            gain_str = "-"
            if d is not None and isinstance(cpd, float) and isinstance(baseline_cpd, float):
                gain = (baseline_cpd - cpd) / baseline_cpd * 100
                gain_str = f"{gain:+.2f}%"
            
            print(f"{label:<15} {cpd:<10} {gain_str:<10}")

if __name__ == "__main__":
    main()
