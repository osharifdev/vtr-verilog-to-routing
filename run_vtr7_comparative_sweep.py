import os
import subprocess
import time
import re
import concurrent.futures
from pathlib import Path
import pandas as pd

# --- Configuration ---
VTR_ROOT = "/home/os717/vtr-verilog-to-routing"
RUN_VTR_FLOW = os.path.join(VTR_ROOT, "vtr_flow/scripts/run_vtr_flow.py")
ARCH = os.path.join(VTR_ROOT, "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BENCH_DIR = os.path.join(VTR_ROOT, "vtr_flow/benchmarks/verilog")

# Official VTR 7.0 Benchmarks (19 Total)
BENCHMARKS = [
    "bgm.v", "blob_merge.v", "boundtop.v", "ch_intrinsics.v", 
    "diffeq1.v", "diffeq2.v", "LU8PEEng.v", "LU32PEEng.v",
    "mcml.v", "mkDelayWorker32B.v", "mkPktMerge.v", "mkSMAdapter4B.v", 
    "or1200.v", "raygentop.v", "sha.v", "stereovision0.v", 
    "stereovision1.v", "stereovision2.v", "stereovision3.v"
]

SEEDS = [1, 2, 3, 4, 5]
LAMBDA = 0.02
DELTA = 20
WORKERS = 32

OUTPUT_DIR = "vtr7_comparative_sweep"

def run_vtr_task(bench_file, seed, mode):
    bench_name = bench_file.split('.')[0]
    # mode can be 'baseline', 'quadratic', 'huber_d20', 'huber_d50'
    run_id = f"{bench_name}_{mode}_s{seed}"
    run_dir = Path(OUTPUT_DIR) / bench_name / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    
    current_delta = DELTA # Default 20
    if mode == 'huber_d50':
        current_delta = 50
    elif mode == 'huber_d20':
        current_delta = 20

    # We use run_vtr_flow.py to handle the full Verilog-to-Routing flow
    cmd = [
        "python3", RUN_VTR_FLOW,
        os.path.join(BENCH_DIR, bench_file),
        ARCH,
        "-start", "odin",
        "-temp_dir", str(run_dir),
        "-route_chan_width", "300"
    ]
    
    vpr_flags = ["--seed", str(seed), "--disp", "off"]
    
    if mode.startswith('huber'):
        vpr_flags += [
            "--prob_timing_enable", "on",
            "--prob_timing_mode", "4",
            "--prob_timing_beta", "1.0",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", str(LAMBDA),
            "--prob_dist_func", "huber",
            "--prob_huber_delta", str(current_delta),
            "--prob_timing_strategy", "off"
        ]
    elif mode == 'quadratic':
        vpr_flags += [
            "--prob_timing_enable", "on",
            "--prob_timing_mode", "4",
            "--prob_timing_beta", "1.0",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_inject_lambda", str(LAMBDA),
            "--prob_dist_func", "quadratic",
            "--prob_timing_strategy", "off"
        ]
    else:
        vpr_flags += ["--prob_timing_enable", "off"]

    cmd += vpr_flags
    # ... rest of function remains same ...

    log_path = run_dir / "run_flow.log"
    try:
        with open(log_path, "w") as f:
            start_time = time.time()
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True, check=True)
            runtime = time.time() - start_time
            
        # Parse CPD from vpr.out (created by run_vtr_flow.py in the temp_dir)
        vpr_out = run_dir / "vpr.out"
        cpd = "PARSE_FAIL"
        if vpr_out.exists():
            with open(vpr_out, "r") as f:
                content = f.read()
                match = re.search(r"Final critical path delay \(least slack\):\s+([0-9.]+)\s+ns", content)
                if match:
                    cpd = float(match.group(1))
        
        return bench_name, seed, mode, cpd, runtime
    except Exception as e:
        return bench_name, seed, mode, "FLOW_FAIL", 0.0

try:
    from tqdm import tqdm
except ImportError:
    # Minimal fallback if tqdm is missing
    class tqdm:
        def __init__(self, total, desc): self.total = total; self.n = 0
        def update(self, n=1): self.n += n; print(f"{desc}: {self.n}/{self.total}")
        def __enter__(self): return self
        def __exit__(self, *args): pass

def main():
    print(f"=== VTR 7.0 COMPARATIVE AUDIT ===")
    print(f"Modes:   Baseline, Quadratic (L={LAMBDA}), Huber_D20, Huber_D50")
    print(f"Targets: {len(BENCHMARKS)} benchmarks x {len(SEEDS)} seeds")
    print(f"Workers: {WORKERS}")
    print("-" * 50)
    
    results = []
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    
    start_time_all = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = []
        for bench in BENCHMARKS:
            for seed in SEEDS:
                for mode in ['baseline', 'quadratic', 'huber_d20', 'huber_d50']:
                    futures.append(executor.submit(run_vtr_task, bench, seed, mode))
        
        total = len(futures)
        with tqdm(total=total, desc="VTR 7.0 Comparative Sweep") as pbar:
            for future in concurrent.futures.as_completed(futures):
                bench, seed, mode, cpd, runtime = future.result()
                results.append({
                    "benchmark": bench,
                    "seed": seed,
                    "mode": mode,
                    "cpd": cpd,
                    "runtime": runtime
                })
                pbar.update(1)

    # Process and Report
    df = pd.DataFrame(results)
    df.to_csv("vtr7_comparative_results.csv", index=False)
    
    # Generate Summary Table
    print("\n" + "="*95)
    print(f"{'Benchmark':<18} | {'Seed':<4} | {'Baseline':>10} | {'Quadratic':>10} | {'H(D=20)':>10} | {'H(D=50)':>10}")
    print("-" * 95)
    
    pivot_df = df.pivot_table(index=['benchmark', 'seed'], columns='mode', values='cpd', aggfunc='first').reset_index()
    pivot_df = pivot_df.sort_values(['benchmark', 'seed'])
    
    for _, row in pivot_df.iterrows():
        b = row['benchmark']
        s = row['seed']
        base = row.get('baseline', 'FAIL')
        quad = row.get('quadratic', 'FAIL')
        h20  = row.get('huber_d20', 'FAIL')
        h50  = row.get('huber_d50', 'FAIL')
        
        def fmt(v): return f"{v:10.4f}" if isinstance(v, float) else f"{str(v):>10}"
        print(f"{b:<18} | {s:<4} | {fmt(base)} | {fmt(quad)} | {fmt(h20)} | {fmt(h50)}")

    # Averages Comparison
    print("-" * 95)
    print(f"{'Benchmark':<18} | AVG  | {'Baseline':>10} | {'Quadratic':>10} | {'H(D=20)':>10} | {'H(D=50)':>10}")
    print("-" * 95)
    
    numeric_df = df[df['cpd'].apply(lambda x: isinstance(x, float))]
    avg_df = numeric_df.groupby(['benchmark', 'mode'])['cpd'].mean().unstack().reset_index()
    
    for _, row in avg_df.iterrows():
        b = row['benchmark']
        base = row.get('baseline', 0)
        quad = row.get('quadratic', 0)
        h20  = row.get('huber_d20', 0)
        h50  = row.get('huber_d50', 0)
        
        print(f"{b:<18} | AVG  | {base:10.4f} | {quad:10.4f} | {h20:10.4f} | {h50:10.4f}")
    
    print("="*85)
    print(f"Total Sweep Runtime: {time.time() - start_time_all:.2f}s")
    print(f"Detailed results saved to: vtr7_comparative_results.csv")

if __name__ == "__main__":
    main()
