import os
import subprocess
import re
import sys

# Paths
VPR_EXEC = "./vpr/vpr"
ARCH_FILE = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BENCHMARK = "vtr_flow/benchmarks/blif/tseng.blif"
ROUTE_CHAN_WIDTH = 100

def run_vpr(beta, alpha=0.1, seed=1):
    cmd = [
        VPR_EXEC, ARCH_FILE, BENCHMARK,
        "--route_chan_width", str(ROUTE_CHAN_WIDTH),
        "--seed", str(seed),
        "--disp", "off",
        "--prob_timing_mode", "4" if beta > 0 else "1", # Mode 4 for Beta>0, Mode 1 for Baseline
        "--prob_timing_alpha", str(alpha),
        "--prob_timing_beta", str(beta), # The new parameter
        "--prob_timing_enable", "on"
    ]
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print("VPR Failed!")
        print(result.stdout) # VPR prints errors to stdout mostly
        print(result.stderr)
        return None

    # Parse Log for specific signals
    log = result.stdout
    
    # 1. Check Mode Logging - find all occurrences and take the last one
    mode_matches = re.findall(r"Mode: (\d+)", log)
    mode = int(mode_matches[-1]) if mode_matches else -1
    
    # 2. Check CPD (Critical Path Delay) - specific to Initial Placement log
    # Example: "Initial placement estimated Critical Path Delay (CPD): 8.25608 ns"
    cpd_match = re.search(r"Initial placement estimated Critical Path Delay \(CPD\): ([\d\.]+) ns", log)
    cpd = float(cpd_match.group(1)) if cpd_match else 0.0
    
    # 3. Check for specific instrumentation
    phys_update_match = re.search(r"INSTRUMENTATION: Physical State Updated", log)
    has_phys_update = bool(phys_update_match)

    return {
        "mode": mode,
        "cpd": cpd,
        "has_phys_update": has_phys_update,
        "log": log
    }

def verify():
    print("--- Verifying Physical Factor Model (Mode 4) ---")
    
    # 1. Baseline (Beta = 0) -> Should behave like Mode 1 
    # (Note: passing beta=0 to the binary with mode=1 effectively)
    print("\n1. Running Baseline (Beta=0)...")
    base_res = run_vpr(beta=0.0, alpha=0.1)
    if not base_res: return

    print(f"   Mode: {base_res['mode']}")
    print(f"   CPD: {base_res['cpd']} ns")
    
    # 2. Physical Model (Beta = 1.0)
    print("\n2. Running Physical Model (Beta=1.0)...")
    phys_res = run_vpr(beta=1.0, alpha=0.1)
    if not phys_res: return

    print(f"   Mode: {phys_res['mode']}")
    print(f"   CPD: {phys_res['cpd']} ns")
    print(f"   Has Physical Update: {phys_res['has_phys_update']}")

    # Checks
    if phys_res['mode'] != 4:
        print("FAIL: Expected Mode 4 execution.")
        sys.exit(1)
        
    if not phys_res['has_phys_update']:
        print("FAIL: Did not see Physical State update logs.")
        sys.exit(1)

    if abs(phys_res['cpd'] - base_res['cpd']) < 1e-9:
        print("WARNING: CPD identical to baseline. This might be okay for Tseng (small), but suggests beta didn't change placement enough.")
    else:
        print(f"SUCCESS: CPD changed (Delta = {phys_res['cpd'] - base_res['cpd']} ns). Physics is affecting placement!")

if __name__ == "__main__":
    verify()
