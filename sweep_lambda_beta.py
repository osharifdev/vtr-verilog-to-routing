
import os
import subprocess
import re
import concurrent.futures
import shutil
import time

# Configuration
VPR_EXEC = os.path.abspath("./vpr/vpr")
ARCH_FILE = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BENCHMARK = os.path.abspath("temp_synth/stereovision2.abc.blif")
ROUTE_CHAN_WIDTH = 300
SEEDS = [1]

# Grid Search Parameters
BETAS = [0.5, 1.0, 2.0, 5.0, 10.0]
LAMBDAS = [0.1, 0.2, 0.5, 1.0, 2.0]
MAX_WORKERS = 25

def run_vpr(beta, lam):
    # Setup unique directory
    run_dir = f"sweep_tuning/beta_{beta}_lambda_{lam}"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    
    cmd = [
        VPR_EXEC,
        ARCH_FILE,
        BENCHMARK,
        "--route_chan_width", str(ROUTE_CHAN_WIDTH),
        "--seed", str(SEEDS[0]),
        "--disp", "off",
        # Core Probabilistic Settings
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4", # Physical Combined
        "--prob_timing_alpha", "0.1",
        "--prob_timing_beta", str(beta),
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
        
        return beta, lam, cpd, None

    except Exception as e:
        return beta, lam, None, str(e)

def main():
    print(f"Starting 5x5 Grid Search on {os.path.basename(BENCHMARK)}")
    print(f"Betas: {BETAS}")
    print(f"Lambdas: {LAMBDAS}")
    print(f"Workers: {MAX_WORKERS}")
    print("-" * 60)
    
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for beta in BETAS:
            for lam in LAMBDAS:
                futures.append(executor.submit(run_vpr, beta, lam))
        
        for future in concurrent.futures.as_completed(futures):
            beta, lam, cpd, error = future.result()
            if cpd:
                print(f"DONE Beta={beta:<4} Lambda={lam:<4} | CPD: {cpd:.4f} ns")
                results.append((beta, lam, cpd))
            else:
                print(f"FAIL Beta={beta:<4} Lambda={lam:<4} | Error: {error}")

    # Generate Grid Report
    print("\n" + "="*60)
    print("GRID SEARCH RESULTS (Beta vs Lambda)")
    print("="*60)
    
    # Baseline for comparison (from previous run)
    BASELINE = 18.6155
    
    # Header
    header = f"{'Beta / Lambda':<15}" + "".join([f"{l:<10}" for l in LAMBDAS])
    print(header)
    print("-" * len(header))
    
    # Rows
    for beta in BETAS:
        row = f"{beta:<15}"
        for lam in LAMBDAS:
            # Find result
            res = next((r[2] for r in results if r[0] == beta and r[1] == lam), None)
            if res:
                # Color code? maybe just value
                gain = ((BASELINE - res) / BASELINE) * 100
                # row += f"{res:.2f} ({gain:+.1f}%) "
                row += f"{res:.2f}     "
            else:
                row += f"{'FAIL':<10}"
        print(row)

    # Find Global Best
    if results:
        best = min(results, key=lambda x: x[2])
        best_gain = ((BASELINE - best[2]) / BASELINE) * 100
        print("\n" + "-"*60)
        print(f"BEST CONFIG: Beta={best[0]}, Lambda={best[1]} -> CPD: {best[2]:.4f} ns ({best_gain:+.2f}%)")
        print("-"*60)

    # Save to file
    with open("tuning_results.md", "w") as f:
        f.write("# Stereovision2 Parameter Tuning (5x5 Grid)\n\n")
        f.write(f"**Baseline CPD**: {BASELINE:.4f} ns\n\n")
        f.write("| Beta \\ Lambda | " + " | ".join([str(l) for l in LAMBDAS]) + " |\n")
        f.write("| :--- | " + " | ".join([":---" for _ in LAMBDAS]) + " |\n")
        
        for beta in BETAS:
            row = f"| **{beta}** |"
            for lam in LAMBDAS:
                res = next((r[2] for r in results if r[0] == beta and r[1] == lam), None)
                if res:
                    gain = ((BASELINE - res) / BASELINE) * 100
                    # Bold if improvement
                    val_str = f"{res:.2f} ns"
                    if gain > 0:
                        val_str = f"**{res:.2f} ns**"
                    row += f" {val_str}<br>({gain:+.2f}%) |"
                else:
                    row += " FAIL |"
            f.write(row + "\n")

    print("\nReport saved to tuning_results.md")

if __name__ == "__main__":
    main()
