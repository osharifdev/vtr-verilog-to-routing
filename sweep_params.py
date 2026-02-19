import subprocess
import os
import re
import concurrent.futures
import shutil
import time

VPR = os.path.abspath("./build/vpr/vpr")
ARCH = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")

BENCHMARKS = {
    "stereovision2": os.path.abspath("reproduce_stereovision2_baseline/stereovision2.pre-vpr.blif"),
    "blob_merge": os.path.abspath("run_blob_baseline/blob_merge.pre-vpr.blif")
}

SEEDS = [1, 4, 13]
LAMBDAS = [0.5, 1.0, 1.5]
ALPHA_CORRS = [0.7, 0.8, 0.9]

def get_cpd(log_file):
    cpd = 0.0
    if not os.path.exists(log_file):
        return 0.0
    with open(log_file, "r") as f:
        for line in f:
            # Match "Final critical path delay (least slack): 17.2928 ns"
            m = re.search(r"Final critical path delay \(least slack\): ([\d\.]+) ns", line)
            if m:
                cpd = float(m.group(1))
            # Fallback to geomean if specific report missed (though crit path is standard)
            if cpd == 0.0:
                m = re.search(r"Final geomean non-virtual intra-domain period: ([\d.]+) ns", line)
                if m:
                    cpd = float(m.group(1))
    return cpd

def run_vpr(benchmark, seed, mode, lambda_val=0.0, alpha_corr=0.0, tolerance=0.05):
    circuit_path = BENCHMARKS[benchmark]
    work_dir = f"sweep_{benchmark}_seed{seed}_{mode}"
    if mode == "prob":
        work_dir += f"_L{lambda_val}_A{alpha_corr}"
    if tolerance != 0.05:
        work_dir += f"_Tol{tolerance}"
    
    os.makedirs(work_dir, exist_ok=True)
    
    # Local blif copy
    circuit_name = os.path.basename(circuit_path)
    shutil.copy2(circuit_path, os.path.join(work_dir, circuit_name))

    cmd = [
        VPR, ARCH, circuit_name,
        "--route_chan_width", "100", # fixed width
        "--seed", str(seed),
        "--disp", "off",
        "--pack", "--place", "--route", "--analysis",
        "--pack", "--place", "--route", "--analysis",
        "--place_algorithm", "criticality_timing",
        "--place_static_cost_tolerance", str(tolerance)
    ]

    if mode == "prob":
        cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_inject", "on",
            "--prob_inject_mode", "regularize",
            "--prob_timing_mode", "3",
            "--prob_timing_strategy", "crit_boost",
            "--prob_timing_alpha", "0.1",
            "--prob_timing_alpha_corr", str(alpha_corr),
            "--prob_inject_lambda", str(lambda_val) 
        ]
        
    log_path = os.path.join(work_dir, "vpr.log")
    with open(log_path, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=work_dir)
        
    return get_cpd(log_path)

def run_sweep(benchmark):
    print(f"=== Sweeping {benchmark} ===")
    
    # 1. Run Baselines
    baselines = {}
    print("  Running Baselines...")
    for seed in SEEDS:
        cpd = run_vpr(benchmark, seed, "base")
        baselines[seed] = cpd
        print(f"    Seed {seed}: {cpd:.4f} ns")

    # 2. Run Parameter Sweep
    results = []
    
    # Flatten tasks
    tasks = []
    for seed in SEEDS:
        if baselines[seed] == 0.0: continue
        for l in LAMBDAS:
            for a in ALPHA_CORRS:
                tasks.append((seed, l, a))
                
    print(f"  Running {len(tasks)} configurations...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(run_vpr, benchmark, s, "prob", l, a, 0.05): (s, l, a) for (s, l, a) in tasks}
        
        for future in concurrent.futures.as_completed(futures):
            s, l, a = futures[future]
            prob_cpd = future.result()
            base_cpd = baselines[s]
            
            if prob_cpd > 0 and base_cpd > 0:
                gain = (base_cpd - prob_cpd) / base_cpd * 100
                results.append((s, l, a, gain))
                # print(f"    Seed {s} L={l} A={a} -> Gain {gain:+.2f}%")
            else:
                results.append((s, l, a, -99.9))

    # 3. Analyze Best Config
    print("\n  --- Best Configurations ---")
    # Group by params
    param_gains = {}
    for s, l, a, g in results:
        k = (l, a)
        if k not in param_gains: param_gains[k] = []
        param_gains[k].append(g)
        
    # Sort by avg gain
    avg_gains = []
    for k, gains in param_gains.items():
        avg = sum(gains) / len(gains)
        avg_gains.append((k, avg, gains))
        
    avg_gains.sort(key=lambda x: x[1], reverse=True)
    
    for (l, a), avg, gains in avg_gains:
        print(f"    Lambda={l} AlphaCorr={a} -> Avg Gain: {avg:+.2f}%  (Seeds: {', '.join([f'{g:+.2f}%' for g in gains])})")

def main():
    for bench in BENCHMARKS:
        if os.path.exists(BENCHMARKS[bench]):
            run_sweep(bench)
        else:
            print(f"Skipping {bench}: file not found")

if __name__ == "__main__":
    main()
