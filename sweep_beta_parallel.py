
import os
import subprocess
import re
import concurrent.futures
import shutil

# Configuration
VPR_EXEC = os.path.abspath("./vpr/vpr")
ARCH_FILE = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BENCHMARK = os.path.abspath("temp_synth/stereovision2.abc.blif")
ROUTE_CHAN_WIDTH = 300
ALPHA = 0.1
SEEDS = [1] # Keep it simple for this broad sweep
BETAS = list(range(11)) # 0 to 10
MAX_WORKERS = 8 # Adjust based on CPU cores

def run_vpr(beta):
    # Setup unique directory
    run_dir = f"sweep_runs/beta_{beta}"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    
    # Mode logic
    mode = 4 if beta > 0 else 1
    
    cmd = [
        VPR_EXEC,
        ARCH_FILE,
        BENCHMARK,
        "--route_chan_width", str(ROUTE_CHAN_WIDTH),
        "--seed", str(SEEDS[0]),
        "--disp", "off",
        "--prob_timing_mode", str(mode),
        "--prob_timing_alpha", str(ALPHA),
        "--prob_timing_beta", str(beta),
        "--prob_timing_enable", "on"
    ]
    
    # print(f"Starts Beta={beta} in {run_dir}")
    
    try:
        # Run inside the directory to contain output files
        with open(os.path.join(run_dir, "vpr_stdout.log"), "w") as f_out, \
             open(os.path.join(run_dir, "vpr_stderr.log"), "w") as f_err:
            
            result = subprocess.run(
                cmd,
                cwd=run_dir,
                stdout=f_out,
                stderr=f_err,
                text=True
            )
            
        # Read back stdout for parsing
        with open(os.path.join(run_dir, "vpr_stdout.log"), "r") as f:
            log_content = f.read()

        if result.returncode != 0:
            return beta, None, f"Exit Code {result.returncode}"
            
        # Parse CPD
        # Matches: "Final critical path delay (least slack): 17.4765 ns"
        match = re.search(r"Final critical path delay.*: ([\d.]+) ns", log_content)
        if match:
            return beta, float(match.group(1)), None
        else:
            return beta, None, "CPD not found"
            
    except Exception as e:
        return beta, None, str(e)

def main():
    print(f"Starting Parallel Beta Sweep (0-10) on {os.path.basename(BENCHMARK)}")
    print(f"Workers: {MAX_WORKERS}")
    print("-" * 40)
    
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_beta = {executor.submit(run_vpr, beta): beta for beta in BETAS}
        
        for future in concurrent.futures.as_completed(future_to_beta):
            beta = future_to_beta[future]
            try:
                beta_res, cpd, error = future.result()
                results.append((beta_res, cpd, error))
                if cpd:
                    print(f"Finished Beta={beta_res:<2} | CPD: {cpd:.4f} ns")
                else:
                    print(f"Finished Beta={beta_res:<2} | FAILED: {error}")
            except Exception as exc:
                print(f"Beta={beta} generated an exception: {exc}")

    # Sort and Print Summary
    results.sort(key=lambda x: x[0])
    
    print("\n" + "="*40)
    print("FINAL RESULTS")
    print("="*40)
    print(f"{'Beta':<5} | {'CPD (ns)':<10} | {'Gain (%)':<10}")
    print("-" * 30)
    
    # Find baseline
    baseline = next((r[1] for r in results if r[0] == 0.0), None)
    
    for beta, cpd, error in results:
        if cpd is None:
            print(f"{beta:<5} | {'FAIL':<10} | -")
            continue
            
        gain_str = "-"
        if baseline and beta != 0.0:
            gain = ((baseline - cpd) / baseline) * 100.0
            gain_str = f"{gain:+.2f}%"
            
        print(f"{beta:<5} | {cpd:<10.4f} | {gain_str}")

if __name__ == "__main__":
    main()
