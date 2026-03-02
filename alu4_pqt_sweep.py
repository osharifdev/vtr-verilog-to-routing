import subprocess
import re
import multiprocessing
import os
import sys
import json
import shutil
from tqdm import tqdm

# ==============================================================================
# CONFIGURATION & PATHS (Absolute derivation)
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VPR_EXE = os.path.join(SCRIPT_DIR, "build/vpr/vpr")
ARCH_FILE = os.path.join(SCRIPT_DIR, "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BLIF_FILE = os.path.join(SCRIPT_DIR, "vtr_flow/benchmarks/blif/6/alu4.blif")
LOG_DIR = os.path.join(SCRIPT_DIR, "pqt_sweep_logs")

# PQT Sweep Parameters
QS_VALS = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
QE_VALS = [0.005, 0.01, 0.02, 0.05, 0.10]

# Generate Grid
RECIPES = []
for qs in QS_VALS:
    for qe in QE_VALS:
        if qs >= qe:
            RECIPES.append({"name": f"PQT_S{qs}_E{qe}", "qs": qs, "qe": qe})

def get_cpd(log_file):
    try:
        with open(log_file, 'r') as f:
            content = f.read()
            # Robust pattern matching for VPR critical path output
            match = re.search(r"Final critical path delay \(least slack\): ([\d.]+) ns", content)
            if not match:
                match = re.search(r"Final critical path: ([\d.]+) ns", content)
            if match:
                return float(match.group(1))
    except:
        pass
    return None

def run_vpr_task(args):
    seed, r, net_file = args
    
    # Sandbox directory to ensure absolute isolation for parallel runs
    # VPR sometimes creates hidden or intermediate files (like alu4.net) locally.
    sandbox = os.path.join(LOG_DIR, f"sandbox_s{seed}_{r['name']}")
    if os.path.exists(sandbox):
        shutil.rmtree(sandbox, ignore_errors=True)
    os.makedirs(sandbox, exist_ok=True)
    
    log_file = os.path.join(LOG_DIR, f"alu4_s{seed}_{r['name']}.log")
    
    # VPR 9.0+ Syntax for Restarting from Netlist:
    # vpr <arch> <blif> --net_file <net> --place --route
    # We MUST provide the .blif as positional 2 to satisfy VPR's format checker.
    cmd = [
        VPR_EXE, ARCH_FILE, BLIF_FILE,
        "--net_file", net_file,
        "--route_chan_width", "100",
        "--disp", "off",
        "--seed", str(seed),
        "--prob_timing_inject", "on",
        "--prob_timing_enable", "on",
        "--prob_timing_mode", "4",
        "--prob_inject_mode", "quantile",
        "--prob_inject_lambda", "0.15",
        "--prob_timing_alpha", "0.10",
        "--prob_timing_beta", "1.0",
        "--prob_inject_quantile_start", str(r["qs"]),
        "--prob_inject_quantile_end", str(r["qe"]),
        "--place",
        "--route",
        "--place_file", "alu4.place", # Local to sandbox
        "--route_file", "alu4.route"  # Local to sandbox
    ]
    
    with open(log_file, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=sandbox)
    
    # Cleanup sandbox to preserve disk space
    shutil.rmtree(sandbox, ignore_errors=True)
    
    return get_cpd(log_file)

def main():
    # 0. Safety Checks
    if not os.path.exists(VPR_EXE):
        print(f"Error: VPR executable not found at {VPR_EXE}")
        print("Please run 'make -j$(nproc) vpr' in the project root.")
        sys.exit(1)

    os.makedirs(LOG_DIR, exist_ok=True)
    seeds = range(1, 17)
    
    print(f"Starting Highly Reliable PQT Optimization Sweep...")
    print(f"Grid: {len(RECIPES)} Configurations x 16 Seeds")

    # 1. Pre-calculate Baselines AND Reference Netlists (Sequential)
    # This phase ensures we have 16 golden packed netlists to start from.
    baselines = {}
    net_files = {}
    print("Pre-calculating golden baselines and netlists...")
    for s in tqdm(seeds, desc="Seed Prep"):
        base_log = os.path.join(LOG_DIR, f"alu4_s{s}_base.log")
        ref_net = os.path.join(LOG_DIR, f"alu4_s{s}_ref.net")
        
        # We always regenerate to ensure freshness and avoid corrupted leftovers
        cmd = [
            VPR_EXE, ARCH_FILE, BLIF_FILE,
            "--route_chan_width", "100",
            "--disp", "off",
            "--seed", str(s),
            "--net_file", ref_net,
            "--pack", "--place", "--route",
            "--place_file", os.path.join(LOG_DIR, f"alu4_s{s}_base.place"),
            "--route_file", os.path.join(LOG_DIR, f"alu4_s{s}_base.route")
        ]
        with open(base_log, "w") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
        
        baselines[s] = get_cpd(base_log)
        net_files[s] = ref_net

    # 2. Run Grid
    grid_results = {}
    oracle_bests = {s: {"name": "Baseline", "cpd": baselines[s], "gain": 0.0} for s in seeds}
    
    for r in RECIPES:
        print(f"\n" + "="*80)
        print(f"EVALUATING CONFIGURATION: {r['name']}")
        print("="*80)
        
        pool = multiprocessing.Pool(processes=min(16, multiprocessing.cpu_count()))
        tasks = [(s, r, net_files[s]) for s in seeds]
        results = pool.map(run_vpr_task, tasks)
        pool.close()
        pool.join()
        
        gains = []
        seed_details = []
        for s, cpd in zip(seeds, results):
            if cpd is not None and baselines[s] is not None:
                gain = (1.0 - cpd / baselines[s]) * 100.0
                gains.append(gain)
                seed_details.append({"seed": s, "base": baselines[s], "best": cpd, "gain": gain})
                
                # Update Oracle Frontier
                if gain > oracle_bests[s]["gain"]:
                    oracle_bests[s] = {"name": r["name"], "cpd": cpd, "gain": gain}
            else:
                seed_details.append({"seed": s, "base": baselines[s], "best": None, "gain": 0.0})
        
        grid_results[r['name']] = {
            "gains": gains,
            "seed_details": seed_details,
            "mean": sum(gains)/len(gains) if gains else 0,
            "max": max(gains) if gains else 0,
            "wins": sum(1 for g in gains if g > 0.001)
        }

        # 2a. Current Config Table
        print(f"\nResults for {r['name']}:")
        print("-" * 65)
        print(f"{'Seed':<6} | {'Baseline (ns)':<15} | {'PQT Result (ns)':<15} | {'Gain %'}")
        print("-" * 65)
        for det in seed_details:
            r_str = f"{det['best']:.4f}" if det['best'] else "FAILED"
            print(f"{det['seed']:<6} | {det['base']:<15.4f} | {r_str:<15} | {det['gain']:<8.2f}%")
        print("-" * 65)
        print(f"CONFIG MEAN GAIN: {grid_results[r['name']]['mean']:.2f}%")

        # 2b. Oracle Frontier Table (Running best-of-all-so-far)
        print(f"\nORACLE FRONTIER (Best Found So Far across ALL configurations):")
        print("-" * 95)
        print(f"{'Seed':<6} | {'Best Configuration':<25} | {'Baseline (ns)':<15} | {'Best PQT (ns)':<15} | {'Gain %'}")
        print("-" * 95)
        oracle_total_gain = 0.0
        for s in seeds:
            ob = oracle_bests[s]
            oracle_total_gain += ob["gain"]
            print(f"{s:<6} | {ob['name']:<25} | {baselines[s]:<15.4f} | {ob['cpd']:<15.4f} | {ob['gain']:<8.2f}%")
        
        oracle_mean = oracle_total_gain / len(seeds)
        print("-" * 95)
        print(f"{'ORACLE MEAN GAIN':<34} | {'Across all 16 seeds':<32} | {oracle_mean:<8.2f}%")
        print("="*95 + "\n")

    # 3. Final Comparison Tables
    print("\n" + "="*95)
    print("FINAL SUMMARY: PQT CONFIGURATION RANKING")
    print(f"{'Configuration':<25} | {'Mean Gain %':<15} | {'Max Gain %':<15} | {'Wins / 16'}")
    print("-" * 95)
    
    sorted_configs = sorted(grid_results.items(), key=lambda x: x[1]['mean'], reverse=True)
    for name, stats in sorted_configs:
        print(f"{name:<25} | {stats['mean']:<15.2f} | {stats['max']:<15.2f} | {stats['wins']}")
    print("-" * 95)

    with open("pqt_sweep_results.json", "w") as f:
        json.dump(grid_results, f, indent=4)

if __name__ == "__main__":
    main()
