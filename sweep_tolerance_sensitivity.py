import subprocess
import os
import re
import shutil
import concurrent.futures

VPR = os.path.abspath("./build/vpr/vpr")
ARCH = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
# Best found so far is Seed 13, L=0.5, A=0.9 (18.15ns)
# Or if test_fix_stereo is better, we might swap.
# Defaulting to Seed 13, L=0.5, A=0.9 for now.
SEED = 13
LAMBDA = 0.5
ALPHA = 0.9

BENCHMARK_SRC = os.path.abspath("vtr_flow/benchmarks/verilog/stereovision2.v")
# Try to find pre-vpr blif if possible
BLIF_PATH = os.path.abspath("sweep_stereovision2_seed13_base/stereovision2.pre-vpr.blif")
if not os.path.exists(BLIF_PATH):
    BLIF_PATH = BENCHMARK_SRC

TOLERANCES = [0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2]

def get_cpd(log_file):
    cpd = 0.0
    if not os.path.exists(log_file):
        return 0.0
    with open(log_file, "r") as f:
        content = f.read()
        if "Routing failed" in content:
            return "Fail_Route"
        m = re.search(r"Final critical path delay.*: ([\d\.]+) ns", content)
        if m:
            cpd = float(m.group(1))
    return cpd

def run_vpr(tol):
    work_dir = f"sweep_tol_sens_T{tol}"
    os.makedirs(work_dir, exist_ok=True)
    
    circuit_name = os.path.basename(BLIF_PATH)
    shutil.copy2(BLIF_PATH, os.path.join(work_dir, circuit_name))
    
    cmd = [
        VPR, ARCH, circuit_name,
        "--route_chan_width", "100", # Need to check CW
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
        "--prob_timing_alpha_corr", str(ALPHA),
        "--prob_inject_lambda", str(LAMBDA),
        "--place_static_cost_tolerance", str(tol)
    ]
    
    # Check if we need specific CW. Stereovision2 is hard.
    # Previous run likely used auto or fixed high CW.
    # I'll let it auto-size if I don't set it? No, VPR usually needs it or does binary search.
    # auto-size is better if we don't know. But for consistent sweep, fixed is better.
    # The sweep_params.py used "300" for Seed 13 verification?
    # Task 25 said "retrying with CW=300".
    # I'll use CW=300 to be safe.
    
    if "route_chan_width" in cmd:
         idx = cmd.index("--route_chan_width")
         cmd[idx+1] = "300"
    
    log_path = os.path.join(work_dir, "vpr.log")
    with open(log_path, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=work_dir)
        
    return get_cpd(log_path)

def main():
    print(f"=== Sweeping Tolerance for Stereovision2 (Seed {SEED}, L={LAMBDA}, A={ALPHA}) ===")
    print(f"tolerances: {TOLERANCES}")
    print(f"{'Tolerance':<15} {'Status':<15} {'CPD (ns)':<15}")
    print("-" * 50)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(run_vpr, t): t for t in TOLERANCES}
        
        for future in concurrent.futures.as_completed(futures):
            t = futures[future]
            res = future.result()
            
            status = "Success" if isinstance(res, float) and res > 0 else str(res)
            val = f"{res:.4f}" if isinstance(res, float) else "N/A"
            
            print(f"{t:<15} {status:<15} {val:<15}")

if __name__ == "__main__":
    main()
