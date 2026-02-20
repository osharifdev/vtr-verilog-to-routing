
import os
import subprocess
import shutil
import re
import time

# Configuration
VPR_EXEC = "./vpr/vpr"
ARCH_FILE = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BENCHMARK_NAME = "diffeq"
# Use diffeq.blif from vtr_flow/benchmarks/blif
CIRCUIT_FILE = "vtr_flow/benchmarks/blif/diffeq.blif" 
ROUTE_CHAN_WIDTH = 50

PROB_FLAGS = [
    "--prob_timing_enable", "on",
    "--prob_timing_strategy", "crit_boost",
    "--prob_timing_alpha", "0.1",
    "--prob_timing_alpha_corr", "0.8",
    "--prob_timing_mc_samples", "1000",
    "--prob_timing_risk_z", "1.645"
]

def run_vpr(run_dir, extra_flags=[]):
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir)
    
    # Check for circuit file
    if not os.path.exists(CIRCUIT_FILE):
        # Fallback to .v if pre-vpr doesn't exist? Actually VPR usually needs .pre-vpr.blif for these benchmarks
        if os.path.exists(BENCHMARK_NAME + ".v"):
             # For now assume failure if not found, usually 
             print(f"Warning: {CIRCUIT_FILE} not found (but expected).")
        pass

    cmd = [
        VPR_EXEC,
        ARCH_FILE,
        BENCHMARK_NAME,
        "--circuit_file", CIRCUIT_FILE,
        "--route_chan_width", str(ROUTE_CHAN_WIDTH)
    ] + extra_flags

    print(f"Running in {run_dir}...")
    print("Command:", " ".join(cmd))
    
    start_time = time.time()
    
    result = subprocess.run(cmd, cwd=os.getcwd(), capture_output=True, text=True)
    
    with open(f"{run_dir}/vpr_stdout.log", "w") as f_out:
        f_out.write(result.stdout)
    with open(f"{run_dir}/vpr_stderr.log", "w") as f_err:
        f_err.write(result.stderr)

    if "FG_DEBUG" in result.stdout:
        print("FG_DEBUG found in output:")
        for line in result.stdout.splitlines():
            if "FG_DEBUG" in line:
                print(line[:100]) # Print first 100 chars
    
    if result.returncode != 0:
        print(f"Error running in {run_dir}: VPR exited with code {result.returncode}")
        print(f"Stderr:\n{result.stderr}")
        return None

    end_time = time.time()
    print(f"Finished in {end_time - start_time:.2f}s")
    
    # Parse WNS
    wns = None
    log_path = f"{run_dir}/vpr.out" # VPR writes to vpr.out in CWD? No, usually not unless redirected.
    # Ah, VPR defaults to writing to stdout or vpr_stdout.log if wrapped.
    # But usually it produces a vpr.out file. Let's check where the output went.
    # My previous script looked at vpr.out in the SUBDIR.
    # Wait, the previous script passed the runs.
    
    # Actually, VPR produces vpr.out in the working directory?
    # Let's assume it produces vpr.out in the current directory, and we move it?
    # Or just parse stdout.
    
    # Let's parse the generated log file.
    if os.path.exists("vpr.out"):
        shutil.move("vpr.out", f"{run_dir}/vpr.out")
        log_path = f"{run_dir}/vpr.out"
    else:
        log_path = f"{run_dir}/vpr_stdout.log"
        
    with open(log_path, 'r') as f:
        content = f.read()
        # Look for "Critical Path: <value> ns" or similar.
        # VPR 7/8 output format: "Final critical path: 1.234 ns"
        # Or "Best Critical Path info: ..."
        
        # Look for "Critical Path:"
        match = re.search(r"Final critical path.*:\s+([\d\.]+)\s+ns", content)
        if match:
            cpd = float(match.group(1))
            wns = -cpd # WNS is negative CPD usually? Or is it Slack?
            # The report says WNS. Usually WNS = Required - Arrival.
            # If Required is 0 (max freq), then WNS = -CPD.
            # But benchmarks usually have a target constraint.
            # Let's assume WNS = -CPD for now as approximation if explicit WNS not found.
            
            # Better check for "Worst Negative Slack" explicitly
            match_wns = re.search(r"Worst Negative Slack \(WNS\):\s+([-\d\.]+)\s+ns", content)
            if match_wns:
                wns = float(match_wns.group(1))
            
    return wns

def main():
    print("Building VPR (just in case)...")
    # subprocess.run(["make", "vpr"], check=True)

    print(f"Verifying {BENCHMARK_NAME}...")

    # Baseline
    wns_base = run_vpr(f"verify_{BENCHMARK_NAME}_base", [])
    
    # Probabilistic
    wns_prob = run_vpr(f"verify_{BENCHMARK_NAME}_prob", PROB_FLAGS)
    
    print("\nResults:")
    if wns_base is not None:
        print(f"Baseline WNS: {wns_base}")
    else:
        print("Baseline Failed")
        
    if wns_prob is not None:
        print(f"Probabilistic WNS: {wns_prob}")
    else:
        print("Probabilistic Failed")

    if wns_base is not None and wns_prob is not None:
        gain = (wns_prob - wns_base) / abs(wns_base) * 100
        print(f"Gain: {gain:.2f}%")
        # ch_intrinsics expected +0.80%
        # diffeq1 expected -0.88%
        
if __name__ == "__main__":
    main()
