import subprocess
import os
import re
import shutil

VPR = os.path.abspath("./build/vpr/vpr")
ARCH = os.path.abspath("vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BENCHMARK = os.path.abspath("reproduce_stereovision2_baseline/stereovision2.pre-vpr.blif")

SEEDS = [1]
LAMBDA = 1.0
ALPHA = 0.8
TOLERANCES = [0.01, 0.05, 0.10, 0.001]

def get_status_and_cpd(log_file):
    cpd = 0.0
    status = "Fail"
    if not os.path.exists(log_file):
        return "NoLog", 0.0
    
    with open(log_file, "r") as f:
        content = f.read()
        if "VPR_ERROR" in content or "Error 1:" in content:
            status = "Error"
        elif "Placement took" in content and "Routing took" in content:
            status = "Success"
        else:
            status = "Crash/Incomplete"
            
        # Extract CPD
        m = re.search(r"Final critical path delay \(least slack\): ([\d\.]+) ns", content)
        if m:
            cpd = float(m.group(1))
            
    return status, cpd

def run_test(tol):
    seed = SEEDS[0]
    work_dir = f"sweep_tol_{tol}"
    os.makedirs(work_dir, exist_ok=True)
    
    circuit_name = os.path.basename(BENCHMARK)
    shutil.copy2(BENCHMARK, os.path.join(work_dir, circuit_name))
    
    cmd = [
        VPR, ARCH, circuit_name,
        "--route_chan_width", "100",
        "--seed", str(seed),
        "--disp", "off",
        "--pack", "--place", "--route", "--analysis",
        "--place_algorithm", "criticality_timing",
        "--place_static_cost_tolerance", str(tol),
        "--prob_timing_enable", "on",
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize",
        "--prob_timing_mode", "3",
        "--prob_timing_strategy", "crit_boost",
        "--prob_timing_alpha", "0.1",
        "--prob_timing_alpha_corr", str(ALPHA),
        "--prob_inject_lambda", str(LAMBDA)
    ]
    
    log_path = os.path.join(work_dir, "vpr.log")
    with open(log_path, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=work_dir)
        
    return get_status_and_cpd(log_path)

def main():
    print(f"=== Sweeping Tolerance for Stereovision2 (Seed {SEEDS[0]}, L={LAMBDA}, A={ALPHA}) ===")
    print(f"{'Tolerance':<15} {'Status':<15} {'CPD (ns)':<15}")
    print("-" * 50)
    
    for tol in TOLERANCES:
        status, cpd = run_test(tol)
        print(f"{tol:<15} {status:<15} {cpd:<15.4f}")

if __name__ == "__main__":
    main()
