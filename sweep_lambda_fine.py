
import os
import subprocess
import re
import concurrent.futures
import shutil
import time

# Configuration
VPR_EXEC = os.path.abspath("./vpr/vpr")
ARCH_FILE = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")

BENCHMARKS = {
    "stereovision2": (os.path.abspath("temp_synth/stereovision2.abc.blif"), 300),
    "bgm": (os.path.abspath("temp_synth/bgm.abc.blif"), 150),
    "blob_merge": (os.path.abspath("temp_synth/blob_merge.abc.blif"), 200),
    "diffeq2": (os.path.abspath("temp_synth/diffeq2.abc.blif"), 100)
}

BETA = 1.0
LAMBDAS = [0.05, 0.1, 0.15, 0.2, 0.25]
SEEDS = [1]
MAX_WORKERS = 20 # 4 benchmarks * 5 lambdas = 20 jobs

def run_vpr(bench_name, lam):
    bench_path, cw = BENCHMARKS[bench_name]
    
    # Setup unique directory
    run_dir = f"sweep_fine/{bench_name}/lambda_{lam}"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    
    cmd = [
        VPR_EXEC,
        ARCH_FILE,
        bench_path,
        "--route_chan_width", str(cw),
        "--seed", str(SEEDS[0]),
        "--disp", "off",
        # Core Probabilistic Settings
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4",
        "--prob_timing_alpha", "0.1",
        "--prob_timing_beta", str(BETA),
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
        
        return bench_name, lam, cpd, None

    except Exception as e:
        return bench_name, lam, None, str(e)

def main():
    print(f"Starting Fine Lambda Sweep (Beta={BETA})")
    print(f"Lambdas: {LAMBDAS}")
    print(f"Benchmarks: {list(BENCHMARKS.keys())}")
    print("-" * 60)
    
    results = {name: {} for name in BENCHMARKS}
    
    # Hardcoded baselines from previous runs for calculation
    BASELINES = {
        "stereovision2": 18.6155,
        "bgm": 18.2324,
        "blob_merge": 10.0687,
        "diffeq2": 17.8208
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for name in BENCHMARKS:
            for lam in LAMBDAS:
                futures.append(executor.submit(run_vpr, name, lam))
        
        for future in concurrent.futures.as_completed(futures):
            name, lam, cpd, error = future.result()
            if cpd:
                print(f"DONE {name:<15} Lambda={lam:<4} | CPD: {cpd:.4f} ns")
                results[name][lam] = cpd
            else:
                print(f"FAIL {name:<15} Lambda={lam:<4} | Error: {error}")

    # Generate Report
    print("\n" + "="*60)
    print("FINE TUNING RESULTS (Beta=1.0)")
    print("="*60)
    
    # Header
    header = f"{'Benchmark':<15} | Base    |" + "".join([f" L={l:<6}|" for l in LAMBDAS])
    print(header)
    print("-" * len(header))
    
    md_table = f"| Benchmark | Base (ns) |" + "".join([f" L={l} |" for l in LAMBDAS]) + "\n"
    md_table += "| :--- | :--- |" + "".join([":--- |" for _ in LAMBDAS]) + "\n"

    for name in BENCHMARKS:
        base = BASELINES.get(name, 0)
        row_str = f"{name:<15} | {base:<7.2f} |"
        md_row = f"| **{name}** | {base:.2f} |"
        
        for lam in LAMBDAS:
            val = results[name].get(lam)
            if val:
                gain = ((base - val) / base) * 100
                row_str += f" {gain:+.2f}% |"
                
                # Highlight best
                cell = f"{val:.2f} ({gain:+.1f}%)"
                if gain > 0: cell = f"**{cell}**"
                md_row += f" {cell} |"
            else:
                row_str += " FAIL   |"
                md_row += " FAIL |"
        
        print(row_str)
        md_table += md_row + "\n"

    # Save to file
    with open("lambda_fine_tuning.md", "w") as f:
        f.write("# Fine-Grained Lambda Tuning\n")
        f.write(f"**Beta**: {BETA}\n\n")
        f.write(md_table)
    
    print("\nReport saved to lambda_fine_tuning.md")

if __name__ == "__main__":
    main()
