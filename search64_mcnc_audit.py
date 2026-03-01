#!/usr/bin/env python3
import subprocess
import re
import argparse
import os
import sys
import statistics
from tqdm import tqdm

# MCNC-20 Benchmark Suite
MCNC_20 = [
    "alu4", "apex2", "apex4", "bigkey", "clma", "des", "diffeq", "dsip",
    "elliptic", "ex1010", "ex5p", "frisc", "misex3", "pdc", "s298",
    "s38417", "s38584.1", "seq", "spla", "tseng"
]

# Configuration
NUM_SEEDS = 16
VPR_BIN = "./vpr/vpr"
ARCH = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BLIF_DIR = "vtr_flow/benchmarks/blif/6"

def run_vpr(benchmark, autonomous=False, seed=1):
    blif_path = os.path.join(BLIF_DIR, f"{benchmark}.blif")
    if not os.path.exists(blif_path):
        return None, f"BLIF not found: {blif_path}"

    cmd = [
        VPR_BIN, ARCH, blif_path,
        "--route_chan_width", "100",
        "--disp", "off",
        "--seed", str(seed)
    ]
    if autonomous:
        cmd += ["--autonomous", "on"]

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output = ""
        for line in process.stdout:
            output += line
        process.wait()

        # Parse CPD (support both standard and autonomous formats)
        m_winner = re.search(r"WINNER: Worker \d+, CPD=([\d.]+)\s+ns", output)
        m_final = re.search(r"Final critical path delay \(least slack\):\s+([\d.]+)\s+ns", output)
        
        if m_winner:
            return float(m_winner.group(1)), None
        elif m_final:
            return float(m_final.group(1)), None
        else:
            return None, "CPD not found in output"
    except Exception as e:
        return None, str(e)

def main():
    parser = argparse.ArgumentParser(description="Search-64 MCNC Audit Script")
    parser.add_argument("benchmarks", nargs="*", help="Specific benchmarks to run (e.g., alu4 clma pdc). If empty, runs all.")
    parser.add_argument("--seeds", type=int, default=16, help="Number of baseline seeds (default 16)")
    args = parser.parse_args()

    targets = args.benchmarks if args.benchmarks else MCNC_20
    num_seeds = args.seeds
    
    print("=" * 72)
    print(f"Search-64 MCNC Audit  |  BASE=avg of {num_seeds} seeds  AUTO=engine best")
    print(f"  Targets: {', '.join(targets) if len(targets) <= 5 else len(targets)}")
    print("=" * 72)

    data = {}
    
    for bench in targets:
        print(f"\n[{bench}]")
        
        # 1. Baseline Sweep
        base_results = []
        pbar_base = tqdm(range(1, num_seeds + 1), desc=f"  Base Sweep", leave=False, unit="seed")
        for s in pbar_base:
            cpd, err = run_vpr(bench, autonomous=False, seed=s)
            if cpd:
                base_results.append(cpd)
            else:
                tqdm.write(f"    Seed {s} FAILED: {err}")
        
        avg_base = statistics.mean(base_results) if base_results else None
        
        # 2. Search-64 Run
        print(f"  Running Search-64 Autonomous Tournament...", end="", flush=True)
        auto_cpd, err = run_vpr(bench, autonomous=True, seed=1)
        if auto_cpd:
            print(f" Done: {auto_cpd:.4f} ns")
        else:
            print(f" FAILED: {err}")

        if avg_base and auto_cpd:
            gain = (1.0 - auto_cpd / avg_base) * 100.0
            tqdm.write(f"  RESULT: Base Avg={avg_base:.4f} ns, Auto={auto_cpd:.4f} ns, Gain={gain:+.2f}%")
            data[bench] = (avg_base, auto_cpd, gain)
        else:
            data[bench] = (avg_base, auto_cpd, None)

    # Summary Table
    print("\n" + "=" * 72)
    print(f"  {'Benchmark':<15} {'Base Avg':>10} {'Auto Best':>10} {'Gain':>10}   Result")
    print("  " + "-" * 68)
    for bench in targets:
        avg_b, auto, gain = data[bench]
        if avg_b and auto:
            tag = "✅ GAIN" if gain > 1.0 else ("❌ REGRESS" if gain < -1.0 else "➡️  NEUTRAL")
            print(f"  {bench:<15} {avg_b:>10.4f} {auto:>10.4f} {gain:>+9.2f}%   {tag}")
        else:
            print(f"  {bench:<15} {'FAIL':>10} {'FAIL':>10} {'N/A':>10}")
    print("=" * 72)

if __name__ == "__main__":
    main()
