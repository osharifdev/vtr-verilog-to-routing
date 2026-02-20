
import os
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm

# --- Configuration ---
VTR_ROOT = "/home/os717/vtr-verilog-to-routing"
RUN_VTR_FLOW = os.path.join(VTR_ROOT, "vtr_flow/scripts/run_vtr_flow.py")
ARCH = os.path.join(VTR_ROOT, "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BENCH_DIR = os.path.join(VTR_ROOT, "vtr_flow/benchmarks/verilog")

# Monster Benchmarks
BENCHMARKS = ["LU32PEEng.v", "mcml.v"]

# Safe channel width for these dense designs
SAFE_W = "200"

# Probabilistic Parameters
PROB_FLAGS = [
    "--prob_timing_enable", "on",
    "--prob_timing_strategy", "crit_boost",
    "--prob_timing_alpha", "0.1",
    "--prob_timing_alpha_corr", "0.8",
    "--prob_timing_mc_samples", "1000",
    "--prob_timing_risk_z", "1.645"
]

def run_vpr_task(bench, strategy):
    bench_path = os.path.join(BENCH_DIR, bench)
    name = bench.split('.')[0]
    output_dir = f"run_vtr7_monster_{name}_{strategy}"
    
    cmd = [
        "python3", RUN_VTR_FLOW,
        bench_path,
        ARCH,
        "-start", "odin",
        "-temp_dir", output_dir,
        "-route_chan_width", SAFE_W
    ]
    
    if strategy == "prob":
        cmd += PROB_FLAGS
    
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
        
        # Parse WNS
        log_path = os.path.join(output_dir, "vpr.out")
        wns = None
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                for line in f:
                    if "Final setup Worst Negative Slack (sWNS):" in line:
                        parts = line.split(":")[-1].strip().split()
                        if parts:
                            wns = float(parts[0])
        
        return {"bench": name, "strategy": strategy, "wns": wns}
    except Exception as e:
        return {"bench": name, "strategy": strategy, "wns": None, "error": str(e)}

def main():
    print(f"=== VTR 7.0 MONSTER AUDIT (LU32 + MCML) ===")
    print(f"Channel Width: {SAFE_W}")
    print("-" * 50)

    results = {}
    tasks = []
    
    # We use 4 workers (2 benchmarks x 2 strategies)
    with ProcessPoolExecutor(max_workers=4) as executor:
        for bench in BENCHMARKS:
            tasks.append(executor.submit(run_vpr_task, bench, "baseline"))
            tasks.append(executor.submit(run_vpr_task, bench, "prob"))
        
        with tqdm(total=len(tasks), desc="Auditing Monsters") as pbar:
            for future in as_completed(tasks):
                res = future.result()
                bench = res["bench"]
                if bench not in results: results[bench] = {}
                results[bench][res["strategy"]] = res["wns"]
                pbar.update(1)

    # Final Report
    print("\n" + "="*60)
    print(f"{'Benchmark':<20} | {'Baseline':>10} | {'Prob':>10} | {'% Gain':>10}")
    print("-"*60)
    
    for bench in sorted(results.keys()):
        base = results[bench].get("baseline")
        prob = results[bench].get("prob")
        
        if base is not None and prob is not None:
            diff = prob - base
            pct = (diff / abs(base)) * 100.0 if base != 0 else 0
            print(f"{bench:<20} | {base:10.4f} | {prob:10.4f} | {pct:+10.2f}%")
        else:
            print(f"{bench:<20} | {'FAIL':>10} | {'FAIL':>10} | {'N/A':>10}")
    print("="*60)

if __name__ == "__main__":
    main()
