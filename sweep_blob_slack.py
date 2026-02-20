
import os
import subprocess
import re
import concurrent.futures
import shutil
import time

# Configuration
VPR_EXEC = os.path.abspath("./vpr/vpr")
ARCH_FILE = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")

# Target Benchmark
BENCH_NAME = "blob_merge"
BENCH_PATH = os.path.abspath("temp_synth/blob_merge.abc.blif")
ROUT_CHAN_WIDTH = 200

# Sweep Parameters
# Now testing higher lambdas because they are dampened by criticality
LAMBDAS = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0]
SEEDS = [1]
MAX_WORKERS = 10 

def run_vpr(lam):
    # Setup unique directory
    run_dir = f"sweep_slack/blob_merge/lambda_{lam}"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    
    cmd = [
        VPR_EXEC,
        ARCH_FILE,
        BENCH_PATH,
        "--route_chan_width", str(ROUT_CHAN_WIDTH),
        "--seed", str(SEEDS[0]),
        "--disp", "off",
        # Core Probabilistic Settings
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4",
        "--prob_timing_alpha", "0.1",
        "--prob_timing_beta", "1.0", # Using the new default
        # Injection Settings
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize", 
        "--prob_inject_lambda", str(lam),
        # Disable interfering strategy
        "--prob_timing_strategy", "off" 
    ]
    
    try:
        # Run inside the directory
        with open(os.path.join(run_dir, "vpr_stdout.log"), "w") as f_out, \
             open(os.path.join(run_dir, "vpr_stderr.log"), "w") as f_err:
            
            result = subprocess.run(
                cmd,
                cwd=run_dir,
                stdout=f_out,
                stderr=f_err,
                text=True
            )
            
        # Parse CPD
        with open(os.path.join(run_dir, "vpr_stdout.log"), "r") as f:
            log_content = f.read()

        match = re.search(r"Final critical path delay.*: ([\d.]+) ns", log_content)
        cpd = float(match.group(1)) if match else None
        
        return lam, cpd, None

    except Exception as e:
        return lam, None, str(e)

def main():
    print(f"Starting Slack-Aware Lambda Sweep for {BENCH_NAME}")
    print(f"Lambdas: {LAMBDAS}")
    print("-" * 60)
    
    results = {}
    
    # Hardcoded baseline
    BASELINE = 10.0687

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for lam in LAMBDAS:
            futures.append(executor.submit(run_vpr, lam))
        
        for future in concurrent.futures.as_completed(futures):
            lam, cpd, error = future.result()
            if cpd:
                gain = ((BASELINE - cpd) / BASELINE) * 100
                print(f"DONE Lambda={lam:<4} | CPD: {cpd:.4f} ns ({gain:+.2f}%)")
                results[lam] = (cpd, gain)
            else:
                print(f"FAIL Lambda={lam:<4} | Error: {error}")

    # Generate Report
    print("\n" + "="*60)
    print("SLACK-AWARE SWEEP RESULTS (Blob_Merge)")
    print("="*60)
    
    sorted_lambdas = sorted(results.keys())
    
    md_table = "| Lambda | CPD (ns) | Gain (%) |\n"
    md_table += "| :--- | :--- | :--- |\n"
    md_table += f"| **Base** | {BASELINE:.4f} | - |\n"

    for lam in sorted_lambdas:
        cpd, gain = results[lam]
        row_str = f"Lambda={lam:<4} | CPD: {cpd:.4f} | Gain: {gain:+.2f}%"
        print(row_str)
        
        cell = f"{gain:+.2f}%"
        if gain > 0: cell = f"**{cell}**"
        md_table += f"| {lam} | {cpd:.4f} | {cell} |\n"

    # Save to file
    with open("blob_slack_results.md", "w") as f:
        f.write("# Slack-Aware Injection Results\n")
        f.write(f"**Benchmark**: {BENCH_NAME}\n")
        f.write(f"**Beta**: 1.0\n\n")
        f.write(md_table)
    
    print("\nReport saved to blob_slack_results.md")

if __name__ == "__main__":
    main()
