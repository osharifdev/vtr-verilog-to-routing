#!/usr/bin/env python3
import subprocess
import os
import re
import concurrent.futures
import shutil
import pandas as pd
import sys

def run_vpr(seed, config, benchmark_info, scout_success_target=-1.0, scout_limit=-1, log_suffix="", is_scout=False):
    root = "/home/os717/vtr-verilog-to-routing"
    vpr_bin = os.path.join(root, "build/vpr/vpr")
    
    arch = os.path.join(root, benchmark_info['arch'])
    circuit = os.path.join(root, benchmark_info['circuit'])
    chan_width = benchmark_info['chan_width']

    config_name = config['name']
    lmb = config['lmb']
    census = config['census']
    boost = config['boost']
    gate = config['gate']

    label = f"scout_{benchmark_info['name']}_{config_name}_s{seed}_{log_suffix}" if is_scout else f"finish_{benchmark_info['name']}_{config_name}_s{seed}_{log_suffix}"
    work_dir = os.path.join(root, f"work_{label}")
    if not os.path.exists(work_dir):
        os.makedirs(work_dir)

    scout_log = os.path.join(work_dir, "scout_telemetry.csv")
    
    cmd = [
        vpr_bin, arch, circuit,
        "--route_chan_width", str(chan_width),
        "--seed", str(seed),
        "--timing_report_detail", "aggregated",
        "--place_quench_only", "off",
        "--prob_timing_enable", "on",
        "--prob_timing_inject", "on",
        "--prob_timing_mode", "4",
        "--prob_inject_mode", "regularize",
        "--prob_self_calibrate", "off",
        "--prob_schedule_ramp", "on",
        "--prob_slack_gate", str(gate),
        "--prob_congestion_gamma", "0.5",
        "--prob_inject_lambda", str(lmb),
        "--prob_census_threshold", str(census),
        "--prob_entropy_sharpening", "on",
        "--prob_momentum_boost", str(boost),
        "--disp", "off"
    ]

    if 'beta' in config:
        cmd += ["--prob_timing_beta", str(config['beta'])]
    if 'alpha' in config:
        cmd += ["--prob_timing_alpha", str(config['alpha'])]
    if 'huber' in config:
        cmd += ["--prob_dist_func", "huber", "--prob_huber_delta", str(config['huber'])]
    if 'self_calibrate' in config:
        cmd += ["--prob_self_calibrate", config['self_calibrate']]

    if is_scout:
        cmd += [
            "--scout_success_target", str(scout_success_target),
            "--scout_limit", str(scout_limit),
            "--scout_log_file", scout_log
        ]

    log_file = os.path.join(work_dir, "vpr.log")

    if not os.path.exists(log_file):
        with open(log_file, "w") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=work_dir)

    # Recovery of results
    cpd = None
    pb_score = 0.0
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            for line in f:
                if "post-quench CPD =" in line:
                    match = re.search(r"post-quench CPD = ([\d\.]+) \(ns\)", line)
                    if match: cpd = float(match.group(1))
                if "Final critical path delay (least slack):" in line:
                    match = re.search(r"Final critical path delay \(least slack\):\s+([\d.]+)\s+ns", line)
                    if match: cpd = float(match.group(1))
    
    if os.path.exists(scout_log):
        try:
            df = pd.read_csv(scout_log, comment='#', names=['Initial','Final','Gain','Momentum','PBScore'])
            if not df.empty:
                pb_score = df['PBScore'].iloc[0]
        except:
            pass

    return (seed, config_name, cpd, pb_score, config)

def get_benchmark(name):
    benchmarks = {
        "bgm": {
            "name": "bgm",
            "arch": "vtr_flow/arch/timing/k6_N10_mem32K_40nm.xml",
            "circuit": "phase4_bgm/bgm_baseline_s1/bgm.pre-vpr.blif",
            "chan_width": 200,
            "baseline": 20.225
        },
        "mkPktMerge": {
            "name": "mkPktMerge",
            "arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml",
            "circuit": "phase6_full_audit/mkPktMerge/mkPktMerge_baseline_s1/mkPktMerge.abc.blif",
            "chan_width": 300,
            "baseline": 4.074 # Mean of 5 seeds from coordinate sweep
        },
        "boundtop": {
            "name": "boundtop",
            "arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml",
            "circuit": "phase5.1_full_audit/boundtop/boundtop_baseline_s1/boundtop.abc.blif",
            "chan_width": 300,
            "baseline": 1.985
        },
        "stereovision3": {
            "name": "stereovision3",
            "arch": "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml",
            "circuit": "phase5.1_full_audit/stereovision3/stereovision3_baseline_s1/stereovision3.abc.blif",
            "chan_width": 300,
            "baseline": 2.41
        }
    }
    return benchmarks.get(name)

def main():
    if len(sys.argv) < 2:
        print("Usage: ./sweep_scouting_portfolio.py <benchmark_name>")
        sys.exit(1)
    
    bench_name = sys.argv[1]
    bench_info = get_benchmark(bench_name)
    if not bench_info:
        print(f"Unknown benchmark: {bench_name}")
        sys.exit(1)

    if bench_name == "bgm":
        CONFIGS = [
            {"name": "Standard-Aggro", "lmb": 0.20, "census": 0.96, "boost": 1.1, "gate": 5e-10},
            {"name": "The-Hammer",     "lmb": 0.50, "census": 0.96, "boost": 1.1, "gate": 5e-10},
            {"name": "The-Scalpel",    "lmb": 0.20, "census": 0.96, "boost": 1.1, "gate": 0.0},
            {"name": "Overdrive",      "lmb": 0.20, "census": 0.96, "boost": 1.5, "gate": 5e-10},
        ]
        SCOUT_LIMIT = 50
    else: # Deep Scouting Suite for mkPktMerge
        CONFIGS = [
            {"name": "λ=0.015", "lmb": 0.015, "census": 0.96, "boost": 1.1, "gate": 5e-10, "beta": 1.0, "alpha": 0.3, "huber": 50.0},
            {"name": "λ=0.020", "lmb": 0.020, "census": 0.96, "boost": 1.1, "gate": 5e-10, "beta": 1.0, "alpha": 0.3, "huber": 50.0},
            {"name": "λ=0.030", "lmb": 0.030, "census": 0.96, "boost": 1.1, "gate": 5e-10, "beta": 1.0, "alpha": 0.3, "huber": 50.0},
            {"name": "Adaptive", "lmb": 0.02, "census": 0.96, "boost": 1.1, "gate": 5e-10, "self_calibrate": "on"},
        ]
        SCOUT_LIMIT = 150
    
    SCOUT_SUCCESS_TARGET = 0.35 
    SEEDS = list(range(1, 17))  

    print(f"=== Robust Portfolio Scouting: {bench_info['name']} (16 Workers) ===")
    print(f"Trigger: Acceptance Rate < {SCOUT_SUCCESS_TARGET}")

    # 1. Scouting Phase
    print(f"\n[PHASE 1] Launching {len(SEEDS) * len(CONFIGS)} Scouts...")
    scout_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = []
        for cfg in CONFIGS:
            for s in SEEDS:
                futures.append(executor.submit(run_vpr, s, cfg, bench_info, SCOUT_SUCCESS_TARGET, SCOUT_LIMIT, "aggro_scout", True))
        
        for f in concurrent.futures.as_completed(futures):
            scout_results.append(f.result())

    # 2. Selection Phase
    scout_results.sort(key=lambda x: x[3], reverse=True)
    print(f"\nScouting Rankings for {bench_info['name']}:")
    print(f"{'Rank':<4} | {'Seed':<5} | {'Config':<15} | {'PB-Score':<10}")
    print("-" * 45)
    for i, (s, name, _, score, _) in enumerate(scout_results[:10]):
        print(f"{i+1:<4} | {s:<5} | {name:<15} | {score:>10.6f}")

    winner = scout_results[0]
    winner_seed, winner_config_name, _, _, winner_config = winner
    print(f"\n🏆 WINNER: Seed {winner_seed} + '{winner_config_name}'")

    # 3. Finalization Phase
    print(f"\n[PHASE 2] Finishing winner...")
    _, _, final_cpd, _, _ = run_vpr(winner_seed, winner_config, bench_info, -1, -1, "winner", False)
    
    BASELINE = bench_info['baseline']
    
    print(f"\n=== FINAL RESULT: {bench_info['name']} ===")
    print(f"Winner Seed   : {winner_seed}")
    print(f"Winner Config : {winner_config_name}")
    if final_cpd is not None:
        print(f"Final CPD     : {final_cpd:.3f} ns")
    else:
        print("Final CPD     : N/A (Run failed or logs were truncated)")
    if final_cpd:
        print(f"Net Gain      : {((BASELINE - final_cpd)/BASELINE)*100:.2f}% (vs Baseline {BASELINE}ns)")

if __name__ == "__main__":
    main()
