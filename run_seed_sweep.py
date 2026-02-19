
import subprocess
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Only focus on blob_merge first as it had the biggest win
BENCHMARK = {
    "name": "blob_merge",
    "path": "run_vtr7_blob_merge_baseline/blob_merge.pre-vpr.blif",
    "chan_width": 100
}

ARCH_FILE = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
VPR_EXEC = "./vpr/vpr"

# Broad sweep
SEEDS = list(range(20)) + [42, 100, 12345]

def get_cpd(output):
    match = re.search(r"Final critical path delay \(least slack\): ([\d\.]+) ns", output)
    if match:
        return float(match.group(1))
    return None

def run_vpr_instance(bench, mode, seed):

    # Convert paths to absolute because we change CWD
    abs_arch = os.path.abspath(ARCH_FILE)
    abs_bench = os.path.abspath(bench["path"])
    abs_vpr = os.path.abspath(VPR_EXEC)

    cmd = [
        abs_vpr,
        abs_arch,
        abs_bench,
        "--route_chan_width", str(bench["chan_width"]),
        "--disp", "off",
        "--seed", str(seed)
    ]

    if mode == "prob":
        cmd.extend([
            "--prob_timing_enable", "on",
            "--prob_timing_strategy", "crit_boost",
            "--prob_timing_alpha", "0.1",
            "--prob_timing_alpha_corr", "0.8",
            "--prob_timing_mc_samples", "1000",
            "--prob_timing_risk_z", "1.645"
        ])
    
    run_dir = f"run_sweep_{bench['name']}_{mode}_seed{seed}"
    os.makedirs(run_dir, exist_ok=True)
    
    start_time = time.time()
    # Run inside the unique directory to avoid file collisions
    result = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True)
    duration = time.time() - start_time
    
    # Save log only if needed (e.g. failure or win)
    # Actually always save basic log
    with open(f"{run_dir}/vpr.log", "w") as f:
        f.write(result.stdout)
        
    if result.returncode != 0:
        return None
        
    return get_cpd(result.stdout)

def check_seed(seed):
    bench = BENCHMARK
    # Run Baseline
    base_cpd = run_vpr_instance(bench, "baseline", seed)
    if not base_cpd:
        return (seed, None, None, None, "Fail Base")
        
    # Run Prob
    prob_cpd = run_vpr_instance(bench, "prob", seed)
    if not prob_cpd:
        return (seed, base_cpd, None, None, "Fail Prob")
        
    delta = (base_cpd - prob_cpd) / base_cpd * 100
    status = "Win" if delta > 0 else "Regress"
    if abs(delta) < 0.05: status = "Neutral"
    
    return (seed, base_cpd, prob_cpd, delta, status)

def main():
    print(f"Sweeping {len(SEEDS)} seeds for {BENCHMARK['name']} using 8 threads...")
    print(f"{'Seed':<5} {'Base (ns)':<10} {'Prob (ns)':<10} {'Gain (%)':<10} {'Status'}")
    print("-" * 50)
    
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_seed = {executor.submit(check_seed, s): s for s in SEEDS}
        for future in as_completed(future_to_seed):
            seed = future_to_seed[future]
            try:
                res = future.result()
                seed_out, base, prob, gain, status = res
                
                if gain is not None:
                     star = "**** MATCH ****" if gain > 5.0 else ""
                     print(f"{seed_out:<5} {base:<10.4f} {prob:<10.4f} {gain:<+10.2f} {status} {star}")
                else:
                     print(f"{seed_out:<5} FAILED {status}")
            except Exception as e:
                print(f"{seed:<5} EXCEPTION: {e}")

if __name__ == "__main__":
    main()
