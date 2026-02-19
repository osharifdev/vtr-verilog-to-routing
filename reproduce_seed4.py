import subprocess
import os
import re
import shutil
import concurrent.futures

VPR = os.path.abspath("./build/vpr/vpr")
ARCH = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BENCHMARK = os.path.abspath("vtr_flow/benchmarks/verilog/diffeq2.v") # Using .v if blif not found, checking paths below.
# Check for pre-vpr blif if available
BLIF_PATH = os.path.abspath("sweep_diffeq2_seed1_base/diffeq2.pre-vpr.blif")
# If not found, check other locations
if not os.path.exists(BLIF_PATH):
    BLIF_PATH = os.path.abspath("vtr_flow/benchmarks/verilog/diffeq2.v")

SEED = 4
LAMBDA = 0.7
ALPHAS = [0.0, 0.5, 0.7, 0.8, 0.9]

def get_cpd(log_file):
    cpd = 0.0
    if not os.path.exists(log_file):
        return 0.0
    with open(log_file, "r") as f:
        content = f.read()
        m = re.search(r"Final critical path delay \(least slack\): ([\d\.]+) ns", content)
        if m:
            cpd = float(m.group(1))
    return cpd

def run_vpr(alpha):
    work_dir = f"repro_seed4_A{alpha}"
    os.makedirs(work_dir, exist_ok=True)
    
    circuit_name = os.path.basename(BLIF_PATH)
    shutil.copy2(BLIF_PATH, os.path.join(work_dir, circuit_name))
    
    cmd = [
        VPR, ARCH, circuit_name,
        "--route_chan_width", "100",
        "--seed", str(SEED),
        "--disp", "off",
        "--pack", "--place", "--route", "--analysis",
        "--place_algorithm", "criticality_timing",
        "--prob_timing_enable", "on",
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize",
        "--prob_timing_mode", "3",
        "--prob_timing_strategy", "crit_boost",
        "--prob_timing_alpha", "0.1",
        "--prob_timing_alpha_corr", str(alpha),
        "--prob_inject_lambda", str(LAMBDA)
    ]
    
    log_path = os.path.join(work_dir, "vpr.log")
    with open(log_path, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=work_dir)
        
    return get_cpd(log_path)

def run_baseline():
    work_dir = f"repro_seed4_baseline"
    os.makedirs(work_dir, exist_ok=True)
    
    circuit_name = os.path.basename(BLIF_PATH)
    shutil.copy2(BLIF_PATH, os.path.join(work_dir, circuit_name))
    
    cmd = [
        VPR, ARCH, circuit_name,
        "--route_chan_width", "100",
        "--seed", str(SEED),
        "--disp", "off",
        "--pack", "--place", "--route", "--analysis",
        "--place_algorithm", "criticality_timing"
    ]
    
    log_path = os.path.join(work_dir, "vpr.log")
    with open(log_path, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=work_dir)
        
    return get_cpd(log_path)

def main():
    print(f"=== Reproducing Seed 4 for Diffeq2 (Lambda={LAMBDA}) ===")
    
    # Run Baseline
    base_cpd = run_baseline()
    print(f"Baseline (Seed {SEED}): {base_cpd:.4f} ns (Target ~17.97)")
    
    # Run Sweep
    print(f"Sweeping Alphas: {ALPHAS}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(run_vpr, a): a for a in ALPHAS}
        
        for future in concurrent.futures.as_completed(futures):
            a = futures[future]
            cpd = future.result()
            gain = (base_cpd - cpd) / base_cpd * 100 if base_cpd > 0 else 0
            target_hit = "YES" if abs(cpd - 17.39) < 0.05 else "NO"
            print(f"Alpha {a}: {cpd:.4f} ns (Gain {gain:+.2f}%) [Target Match: {target_hit}]")

if __name__ == "__main__":
    main()
