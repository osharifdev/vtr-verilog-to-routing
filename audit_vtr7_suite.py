#!/usr/bin/env python3
import subprocess
import os
import re
import time
import json
import sys

# =============================================================================
# VTR 7.0 Suite Audit Script (Phase 18: Recursive Scouting)
# =============================================================================

BENCHMARKS = [
    "bgm", "blob_merge", "boundtop", "ch_intrinsics", "diffeq",
    "LU8PEEng", "LU32PEEng", "mcml", "mkDelayWorker32B", "mkPktMerge",
    "mkSMAdapter4B", "or1200", "raygentop", "sha", "stereovision0",
    "stereovision1", "stereovision2", "stereovision3"
]

ARCH = "./vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
VPR = "./build/vpr/vpr"

def run_cmd(cmd, verbose=False):
    if verbose:
        print(f"Executing: {cmd}")
    process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    return stdout.decode(), stderr.decode(), process.returncode

def parse_vpr_log(filename):
    cpd = "N/A"
    runtime = "N/A"
    if not os.path.exists(filename):
        return cpd, runtime
    with open(filename, 'r') as f:
        content = f.read()
        # Parse CPD
        cpd_match = re.search(r"Final critical path delay \(least slack\): ([\d.]+) ns", content)
        if cpd_match:
            cpd = cpd_match.group(1)
        
        # Parse Runtime
        runtime_match = re.search(r"The entire flow of VPR took ([\d.]+) seconds", content)
        if runtime_match:
            runtime = runtime_match.group(1)
    return cpd, runtime

def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=40, fill='█', printEnd="\r"):
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    sys.stdout.write(f'\r{prefix} |{bar}| {percent}% {suffix}')
    sys.stdout.flush()
    if iteration == total:
        print()

def find_blif(bench):
    # Search priority: 
    # 1. vtr7_blifs/
    # 2. vtr_flow/benchmarks/blif/
    # 3. benchmarks/
    # 4. vtr_flow/benchmarks/vtr_benchmarks_blif/
    candidates = [
        f"./vtr7_blifs/{bench}.blif",
        f"./vtr_flow/benchmarks/blif/{bench}.blif",
        f"./benchmarks/{bench}.blif",
        f"./vtr_flow/benchmarks/vtr_benchmarks_blif/{bench}.blif"
    ]
    # Handle diffeq specific case
    if bench == "diffeq":
        candidates.append("./vtr_flow/benchmarks/blif/diffeq.blif")

    for path in candidates:
        if os.path.exists(path):
            return path
    return None

def audit_benchmark(bench, verbose=False):
    blif_path = find_blif(bench)
    if not blif_path:
        if verbose: print(f"Error: Could not find BLIF for {bench}")
        return None

    # 1. VPR Baseline Run
    log_base = f"audit_base_{bench}.log"
    vpr_base_cmd = f"{VPR} {ARCH} {blif_path} > {log_base} 2>&1"
    run_cmd(vpr_base_cmd, verbose)
    
    # 2. VPR Recursive Scouting Run (Phase 18)
    log_auto = f"audit_auto_{bench}.log"
    vpr_auto_cmd = f"{VPR} {ARCH} {blif_path} --autonomous on > {log_auto} 2>&1"
    run_cmd(vpr_auto_cmd, verbose)

    # 3. Extract Results
    base_cpd, base_time = parse_vpr_log(log_base)
    auto_cpd, auto_time = parse_vpr_log(log_auto)
    
    try:
        gain = (1.0 - (float(auto_cpd) / float(base_cpd))) * 100.0
        overhead = (float(auto_time) / float(base_time) - 1.0) * 100.0
    except:
        gain = 0.0
        overhead = 0.0

    return {
        "Benchmark": bench,
        "Base CPD": base_cpd,
        "Auto CPD": auto_cpd,
        "Gain %": f"{gain:.2f}%",
        "Base Time": base_time,
        "Auto Time": auto_time,
        "Overhead %": f"{overhead:.1f}%"
    }

def main():
    verbose = "--debug" in sys.argv
    if not os.path.exists(VPR):
        print("Error: VPR binary not found in build/vpr/vpr. Please build the project.")
        sys.exit(1)

    total_benchmarks = len(BENCHMARKS)
    results = []
    
    print(f"Starting VTR 7.0 Suite Audit ({total_benchmarks} benchmarks)...")
    print_progress_bar(0, total_benchmarks, prefix='Progress:', suffix='Ready', length=50)

    for i, bench in enumerate(BENCHMARKS):
        res = audit_benchmark(bench, verbose)
        if res:
            results.append(res)
        
        print_progress_bar(i + 1, total_benchmarks, prefix='Progress:', suffix=f'({bench} completed)', length=50)

    # Final Report Generation
    with open("vtr7_audit_report.md", 'w') as f:
        f.write("# VTR 7.0 Suite Comprehensive PPA Audit (Phase 18)\n\n")
        f.write("| Benchmark | Base CPD (ns) | Auto CPD (ns) | Gain % | Base Time (s) | Auto Time (s) | Overhead % |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for r in results:
            f.write(f"| {r['Benchmark']} | {r['Base CPD']} | {r['Auto CPD']} | {r['Gain %']} | {r['Base Time']} | {r['Auto Time']} | {r['Overhead %']} |\n")

    print("\n[DONE] Final results saved to vtr7_audit_report.md")

if __name__ == "__main__":
    main()
