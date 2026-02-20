import subprocess
import re
import os
import concurrent.futures
from pathlib import Path

# Targeted Sweep Parameters
BENCHMARKS = ["blob_merge", "stereovision2", "bgm", "diffeq2"]
LAMBDAS = [0.05, 0.1, 0.15, 0.2, 0.25]
BETA = 1.0

VPR_PATH = "/home/os717/vtr-verilog-to-routing/vpr/vpr"
ARCH = "/home/os717/vtr-verilog-to-routing/vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BLIF_DIR = "/home/os717/vtr-verilog-to-routing/temp_synth"
OUTPUT_DIR = "sweep_slack_targeted"

def run_vpr(bench, l):
    run_dir = Path(OUTPUT_DIR) / bench / f"l_{l}"
    run_dir.mkdir(parents=True, exist_ok=True)
    
    log_path = run_dir / "vpr_stdout.log"
    
    cmd = [
        VPR_PATH, ARCH, f"{BLIF_DIR}/{bench}.abc.blif",
        "--route_chan_width", "300",
        "--seed", "1",
        "--disp", "off",
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4",
        "--prob_timing_beta", str(BETA),
        "--prob_timing_inject", "on",
        "--prob_inject_mode", "regularize",
        "--prob_inject_lambda", str(l),
        "--prob_timing_strategy", "off"
    ]
    
    try:
        with open(log_path, "w") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(run_dir), check=True)
        
        # Parse CPD
        with open(log_path, "r") as f:
            content = f.read()
            match = re.search(r"Final critical path delay \(least slack\):\s+([0-9.]+)\s+ns", content)
            if match:
                cpd = float(match.group(1))
                return bench, l, cpd
            else:
                return bench, l, "PARSE_FAIL"
    except subprocess.CalledProcessError:
        return bench, l, "VPR_FAIL"

def main():
    print(f"Starting Targeted Slack-Aware Sweep (Beta={BETA})")
    print(f"Lambdas: {LAMBDAS}")
    
    results = {}
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_vpr = {executor.submit(run_vpr, b, l): (b, l) for b in BENCHMARKS for l in LAMBDAS}
        for future in concurrent.futures.as_completed(future_to_vpr):
            bench, l, res = future.result()
            if bench not in results:
                results[bench] = {}
            results[bench][l] = res
            print(f"DONE {bench:<15} L={l:<6} | Result: {res}")

    # Reference Baselines (Approximate from previous logs)
    # stereovision2 base: ~18.62
    # blob_merge base: ~10.07
    refs = {"stereovision2": 18.62, "blob_merge": 10.07}

    print("\nFinal Results Table:")
    header = "Benchmark".ljust(15) + "".join([f"L={l:<8}" for l in LAMBDAS])
    print(header)
    print("-" * len(header))
    
    for b in BENCHMARKS:
        row = f"{b:<15}"
        for l in LAMBDAS:
            val = results[b].get(l, "N/A")
            if isinstance(val, float):
                ref = refs.get(b, 1.0)
                diff = (1.0 - val/ref) * 100
                row += f"{val:<8.4f} ({diff:>+5.1f}%) "
            else:
                row += f"{str(val):<8}        "
        print(row)

if __name__ == "__main__":
    main()
