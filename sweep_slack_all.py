
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
    "stereovision2": (os.path.abspath("temp_synth/stereovision2.abc.blif"), 300, 18.6174),
    "blob_merge": (os.path.abspath("temp_synth/blob_merge.abc.blif"), 200, 10.0687),
}

LAMBDAS = [0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 1.0]
SEEDS = [1]
MAX_WORKERS = 16 # 2 benches * 8 lambdas = 16 jobs

def run_vpr(bench_name, path, cw, baseline, lam):
    run_dir = f"sweep_slack_all/{bench_name}/l_{lam}"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    
    cmd = [
        VPR_EXEC,
        ARCH_FILE,
        path,
        "--route_chan_width", str(cw),
        "--seed", "1",
        "--disp", "off",
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4",
        "--prob_timing_beta", "1.0",
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize",
        "--prob_inject_lambda", str(lam),
        "--prob_timing_strategy", "off"
    ]
    
    try:
        with open(os.path.join(run_dir, "vpr_stdout.log"), "w") as f_out:
            subprocess.run(cmd, cwd=run_dir, stdout=f_out, stderr=subprocess.DEVNULL)
            
        with open(os.path.join(run_dir, "vpr_stdout.log"), "r") as f:
            log = f.read()
        
        match = re.search(r"Final critical path delay.*: ([\d.]+) ns", log)
        cpd = float(match.group(1)) if match else None
        return bench_name, lam, cpd, baseline
    except:
        return bench_name, lam, None, baseline

def main():
    print("Starting Slack-Aware Sweep All")
    results = {b: {} for b in BENCHMARKS}
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for b, (path, cw, base) in BENCHMARKS.items():
            for lam in LAMBDAS:
                futures.append(executor.submit(run_vpr, b, path, cw, base, lam))
        
        for future in concurrent.futures.as_completed(futures):
            b, lam, cpd, base = future.result()
            if cpd:
                gain = ((base - cpd) / base) * 100
                print(f"DONE {b:<15} L={lam:<4} | CPD: {cpd:.4f} ({gain:+.2f}%)")
                results[b][lam] = gain
            else:
                print(f"FAIL {b:<15} L={lam:<4}")

    print("\nSUMMARY (Gains %)")
    header = "| Benchmark | " + " | ".join([f"L={l}" for l in LAMBDAS]) + " |"
    print(header)
    for b in BENCHMARKS:
        row = f"| {b:<13} | "
        for l in LAMBDAS:
            gain = results[b].get(l, 0.0)
            row += f"{gain:+.2f}% | "
        print(row)

if __name__ == "__main__":
    main()
