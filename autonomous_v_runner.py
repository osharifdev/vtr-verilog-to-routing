import subprocess
import os
import re
import concurrent.futures
import pandas as pd
import sys
import time
import shutil
from tqdm import tqdm

VTR_ROOT = "/home/os717/vtr-verilog-to-routing"
VPR_BIN = os.path.join(VTR_ROOT, "build/vpr/vpr")
WORK_ROOT = os.path.join(VTR_ROOT, "autonomous_v6_overhead_test")

# The Elite 16 (Ranked by Robustness in Audit V5)
ELITE_16 = {
    30: {"lmb": 0.05,  "beta": 1.0, "alpha": 0.10, "gate": 0.0,    "boost": 1.1},
    5:  {"lmb": 0.10,  "beta": 1.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1},
    31: {"lmb": 0.20,  "beta": 1.0, "alpha": 0.10, "gate": 0.0,    "boost": 1.1},
    4:  {"lmb": 0.05,  "beta": 1.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1},
    6:  {"lmb": 0.20,  "beta": 1.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1},
    20: {"lmb": 0.005, "beta": 2.0, "alpha": 0.05, "gate": 5e-10,  "boost": 1.1},
    29: {"lmb": 1.00,  "beta": 1.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1},
    2:  {"lmb": 0.01,  "beta": 1.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1},
    3:  {"lmb": 0.02,  "beta": 1.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1},
    22: {"lmb": 0.05,  "beta": 2.0, "alpha": 0.05, "gate": 5e-10,  "boost": 1.1},
    18: {"lmb": 0.20,  "beta": 1.0, "alpha": 0.05, "gate": 5e-10,  "boost": 1.1},
    15: {"lmb": 0.005, "beta": 1.0, "alpha": 0.20, "gate": 5e-10,  "boost": 1.1},
    9:  {"lmb": 0.005, "beta": 2.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1},
    14: {"lmb": 0.005, "beta": 1.0, "alpha": 0.05, "gate": 5e-10,  "boost": 1.1},
    28: {"lmb": 0.50,  "beta": 2.0, "alpha": 0.05, "gate": 0.0,    "boost": 1.1},
    7:  {"lmb": 0.50,  "beta": 1.0, "alpha": 0.10, "gate": 5e-10,  "boost": 1.1}
}

BENCHMARKS = {
    "bgm": {"arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml", "circuit": "benchmarks/bgm.blif", "chan_width": 300, "blocks": 31909},
    "ch_intrinsics": {"arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml", "circuit": "benchmarks/ch_intrinsics.blif", "chan_width": 300, "blocks": 493},
    "mkPktMerge": {"arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml", "circuit": "benchmarks/mkPktMerge.blif", "chan_width": 300, "blocks": 1194},
    "diffeq1": {"arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml", "circuit": "benchmarks/diffeq1.blif", "chan_width": 300, "blocks": 4824},
    "diffeq2": {"arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml", "circuit": "benchmarks/diffeq2.blif", "chan_width": 300, "blocks": 590},
    "stereovision3": {"arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml", "circuit": "benchmarks/stereovision3.blif", "chan_width": 300, "blocks": 323},
    "boundtop": {"arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml", "circuit": "benchmarks/boundtop.blif", "chan_width": 300, "blocks": 648},
}

def run_vpr_scout(bench_name, config_id, seed, is_scout=True, scout_limit=50, scout_success_target=0.35):
    bench = BENCHMARKS[bench_name]
    config = ELITE_16[config_id]
    
    label = f"{bench_name}_C{config_id}_S{seed}"
    work_dir = os.path.join(WORK_ROOT, bench_name, label)
    os.makedirs(work_dir, exist_ok=True)
    
    scout_log = os.path.join(work_dir, "scout_telemetry.csv")
    
    cmd = [
        VPR_BIN, os.path.join(VTR_ROOT, bench['arch']), os.path.join(VTR_ROOT, bench['circuit']),
        "--route_chan_width", str(bench['chan_width']),
        "--seed", str(seed),
        "--prob_timing_enable", "on",
        "--prob_timing_inject", "on",
        "--prob_timing_mode", "4",
        "--prob_inject_mode", "regularize",
        "--prob_inject_lambda", str(config['lmb']),
        "--prob_timing_beta", str(config['beta']),
        "--prob_timing_alpha", str(config['alpha']),
        "--prob_slack_gate", str(config['gate']),
        "--prob_momentum_boost", str(config['boost']),
        "--prob_congestion_gamma", "0.5",
        "--prob_census_threshold", "0.96",
        "--disp", "off"
    ]
    
    if is_scout:
        cmd += [
            "--scout_limit", str(scout_limit),
            "--scout_success_target", str(scout_success_target),
            "--scout_log_file", scout_log
        ]
    
    log_file = os.path.join(work_dir, "vpr.log")
    with open(log_file, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=work_dir)

    pb_score = 0.0
    if os.path.exists(scout_log):
        try:
            sdf = pd.read_csv(scout_log, comment='#', names=['Initial','Final','Gain','Momentum','PBScore'])
            if not sdf.empty:
                pb_score = sdf['PBScore'].iloc[-1]
        except: pass
    
    cpd = None
    if not is_scout:
        if os.path.exists(log_file):
            with open(log_file, "r") as f:
                for line in f:
                    if "Final critical path delay (least slack):" in line:
                        match = re.search(r"Final critical path delay \(least slack\):\s+([\d.]+)\s+ns", line)
                        if match: cpd = float(match.group(1))
    
    return {"benchmark": bench_name, "config_id": config_id, "seed": seed, "pb_score": pb_score, "cpd": cpd}

def process_benchmark(bench_name):
    print(f"\n>>> AUDITING OVERHEAD for: {bench_name}")
    bench_dir = os.path.join(WORK_ROOT, bench_name)
    if os.path.exists(bench_dir):
        shutil.rmtree(bench_dir)
    os.makedirs(bench_dir)
    
    num_blocks = BENCHMARKS[bench_name]["blocks"]
    if num_blocks < 2000:
        scout_limit = 15
        scout_success_target = 0.50
    elif num_blocks < 10000:
        scout_limit = 25
        scout_success_target = 0.45
    else:
        scout_limit = 50
        scout_success_target = 0.35
    
    # --- STAGE 1: Search Sprint ---
    print(f"    [STAGE 1] Searching Elite 16 (Limit={scout_limit})...")
    start_s1 = time.time()
    scout_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(run_vpr_scout, bench_name, cid, 1, True, scout_limit, scout_success_target): cid for cid in ELITE_16}
        for f in concurrent.futures.as_completed(futures):
            scout_results.append(f.result())
    end_s1 = time.time()
    s1_duration = end_s1 - start_s1
    
    scout_df = pd.DataFrame(scout_results).sort_values("pb_score", ascending=False)
    top_2 = scout_df.head(2)["config_id"].tolist()
    
    # --- STAGE 2: Breakout Attack ---
    print(f"    [STAGE 2] Exploing Top 2 Winners (8 seeds each)...")
    start_s2 = time.time()
    final_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        for cid in top_2:
            for s in range(1, 9):
                final_results.append(executor.submit(run_vpr_scout, bench_name, cid, s, False))
        final_results = [f.result() for f in final_results]
    end_s2 = time.time()
    s2_duration = end_s2 - start_s2
    
    best_row = pd.DataFrame(final_results).dropna(subset=["cpd"]).sort_values("cpd").iloc[0]
    
    overhead = s1_duration / s2_duration
    total_overhead = (s1_duration + s2_duration) / s2_duration
    
    print(f"    S1 Duration: {s1_duration:.1f}s")
    print(f"    S2 Duration: {s2_duration:.1f}s (Average for full run)")
    print(f"    LATENCY OVERHEAD: {total_overhead:.3f}x (+{ (total_overhead-1)*100:.1f}%)")
    
    return {
        "benchmark": bench_name,
        "s1_sec": s1_duration,
        "s2_sec": s2_duration,
        "overhead": total_overhead,
        "cpd": best_row["cpd"]
    }

def main():
    os.makedirs(WORK_ROOT, exist_ok=True)
    all_summary = []
    for bn in BENCHMARKS:
        res = process_benchmark(bn)
        if res is not None:
            all_summary.append(res)
            
    summary_df = pd.DataFrame(all_summary)
    print("\n--- AUTONOMOUS SEARCH-EXPLOIT OVERHEAD REPORT ---")
    print(summary_df[["benchmark", "s1_sec", "s2_sec", "overhead", "cpd"]].to_string(index=False))
    summary_df.to_csv(os.path.join(WORK_ROOT, "overhead_audit_summary.csv"), index=False)

if __name__ == "__main__":
    main()
