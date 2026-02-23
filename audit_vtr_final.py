import os
import subprocess
import time
import re

VPR_BIN = "./vpr/vpr"
ARCH = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
BENCHMARKS = [
    ("elliptic", "vtr_flow/benchmarks/blif/elliptic.blif"),
    ("ex5p", "vtr_flow/benchmarks/blif/ex5p.blif"),
    ("apex2", "vtr_flow/benchmarks/blif/apex2.blif"),
    ("apex4", "vtr_flow/benchmarks/blif/apex4.blif"),
    ("alu4", "vtr_flow/benchmarks/blif/alu4.blif"),
    ("des", "vtr_flow/benchmarks/blif/des.blif"),
    ("seq", "vtr_flow/benchmarks/blif/seq.blif")
]

results = []

def run_vpr(bench_info, autonomous=False):
    name, path = bench_info
    cmd = [
        VPR_BIN, ARCH, path,
        "--route_chan_width", "300",
        "--disp", "off"
    ]
    if autonomous:
        cmd += ["--autonomous", "on"]
    else:
        cmd += ["--num_workers", "1"]

    print(f"Running: {' '.join(cmd)}")
    start_time = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = proc.communicate()
        end_time = time.time()
        runtime = end_time - start_time
        
        # Parse CPD (Try Geomean first, then fallback to least slack)
        cpd_match = re.search(r"Final geomean non-virtual intra-domain period: ([\d.]+) ns", stdout)
        if cpd_match:
            cpd = float(cpd_match.group(1))
        else:
            cpd_match = re.search(r"Final critical path delay \(least slack\): ([\d.]+) ns", stdout)
            cpd = float(cpd_match.group(1)) if cpd_match else 0.0
        
        return cpd, runtime, stdout
    except Exception as e:
        print(f"Error running {name}: {e}")
        return 0.0, 0.0, ""

print("Benchmark\tType\tCPD (ns)\tRuntime (s)")
for bench_info in BENCHMARKS:
    name, path = bench_info
    # Baseline
    b_cpd, b_time, _ = run_vpr(bench_info, autonomous=False)
    # Autonomous
    a_cpd, a_time, _ = run_vpr(bench_info, autonomous=True)
    
    results.append({
        "name": name,
        "b_cpd": b_cpd,
        "b_time": b_time,
        "a_cpd": a_cpd,
        "a_time": a_time
    })
    
    print(f"{name}\tBase\t{b_cpd:.3f}\t{b_time:.1f}")
    print(f"{name}\tAuto\t{a_cpd:.3f}\t{a_time:.1f}")

# Final Table Generation
print("\n" + "="*80)
print(f"{'Benchmark':<15} {'Base CPD':<10} {'Auto CPD':<10} {'CPD Red.':<10} {'Gain (%)':<10} {'Overhead (%)':<10}")
print("-" * 80)

for res in results:
    cpd_red = res["a_cpd"] - res["b_cpd"]
    gain = (1 - res["a_cpd"] / res["b_cpd"]) * 100 if res["b_cpd"] > 0 else 0
    overhead = (res["a_time"] / res["b_time"] - 1) * 100 if res["b_time"] > 0 else 0
    
    print(f"{res['name']:<15} {res['b_cpd']:<10.3f} {res['a_cpd']:<10.3f} {cpd_red:<10.3f} {gain:<10.2f}% {overhead:<10.2f}%")
