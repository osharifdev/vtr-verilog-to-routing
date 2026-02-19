
import os
import subprocess
import re
import concurrent.futures
import shutil
import time

# Configuration
VPR_EXEC = os.path.abspath("./vpr/vpr")
ARCH_FILE = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")

# Benchmarks map: Name -> (Path, RoutChanWidth)
BENCHMARKS = {
    "stereovision2": (os.path.abspath("temp_synth/stereovision2.abc.blif"), 300),
    "bgm": (os.path.abspath("temp_synth/bgm.abc.blif"), 150),       # Guessing routing width, VPR will error if too low
    "blob_merge": (os.path.abspath("temp_synth/blob_merge.abc.blif"), 200), # Guessing
    "diffeq2": (os.path.abspath("temp_synth/diffeq2.abc.blif"), 100)       # Guessing
}

ALPHA = 0.1
LAMBDA = 1.0
SEEDS = [1]
BETAS = [0.0, 0.5, 1.0, 2.0, 5.0, 10.0]
MAX_WORKERS = 12 # 4 benchmarks * 6 betas = 24 jobs total. 12 workers is reasonable.

def run_vpr(bench_name, beta):
    bench_path, cw = BENCHMARKS[bench_name]
    
    # Setup unique directory
    run_dir = f"sweep_all/{bench_name}/beta_{beta}"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    
    # Mode logic
    mode = 4 if beta > 0 else 1
    
    cmd = [
        VPR_EXEC,
        ARCH_FILE,
        bench_path,
        "--route_chan_width", str(cw),
        "--seed", str(SEEDS[0]),
        "--disp", "off",
        # Core Probabilistic Settings
        "--prob_timing_enable", "on",
        "--prob_timing_mode", str(mode),
        "--prob_timing_alpha", str(ALPHA),
        "--prob_timing_beta", str(beta),
        # Injection Settings
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize", 
        "--prob_inject_lambda", str(LAMBDA),
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
        
        return bench_name, beta, cpd, None

    except Exception as e:
        return bench_name, beta, None, str(e)

def main():
    print(f"Starting Comprehensive Sweep")
    print("-" * 40)
    
    results = {name: {} for name in BENCHMARKS}
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for name in BENCHMARKS:
            for beta in BETAS:
                futures.append(executor.submit(run_vpr, name, beta))
        
        for future in concurrent.futures.as_completed(futures):
            name, beta, cpd, error = future.result()
            if cpd:
                print(f"DONE {name:<15} Beta={beta:<4} | CPD: {cpd:.4f} ns")
                results[name][beta] = cpd
            else:
                print(f"FAIL {name:<15} Beta={beta:<4} | Error: {error}")
                results[name][beta] = None

    # Markdown Report Generation
    print("\nGenerating Report...")
    with open("benchmark_results.md", "w") as f:
        f.write("# Comprehensive Physical Factor Model Results\n\n")
        f.write(f"**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("**Configuration:** Injection=ON (Regularize, Lambda=1.0), Strategy=OFF\n\n")
        
        f.write("| Benchmark | Beta | CPD (ns) | Gain (%) |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")
        
        for name in BENCHMARKS:
            baseline = results[name].get(0.0)
            
            # Print Baseline first
            if baseline:
                f.write(f"| **{name}** | 0.0 | {baseline:.4f} | - |\n")
            else:
                f.write(f"| **{name}** | 0.0 | FAIL | - |\n")
                
            # Print others
            for beta in sorted(BETAS):
                if beta == 0.0: continue
                val = results[name].get(beta)
                
                if val and baseline:
                    gain = ((baseline - val) / baseline) * 100.0
                    f.write(f"| | {beta} | {val:.4f} | {gain:+.2f}% |\n")
                elif val:
                    f.write(f"| | {beta} | {val:.4f} | ? |\n")
                else:
                    f.write(f"| | {beta} | FAIL | |\n")
            
            f.write("| | | | |\n") # Spacer row

    print("Report saved to benchmark_results.md")

if __name__ == "__main__":
    main()
