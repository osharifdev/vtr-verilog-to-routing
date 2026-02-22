#!/usr/bin/env python3
import subprocess
import os
import re
import concurrent.futures
import shutil
import statistics
import pandas as pd

def run_vpr(seed, config, scout_success_target=-1.0, scout_limit=-1, log_suffix="", is_scout=False):
    root = "/home/os717/vtr-verilog-to-routing"
    vpr_bin = os.path.join(root, "build/vpr/vpr")
    arch = os.path.join(root, "vtr_flow/arch/timing/k6_N10_mem32K_40nm.xml")
    circuit = os.path.join(root, "phase4_bgm/bgm_baseline_s1/bgm.pre-vpr.blif")

    config_name = config['name']
    lmb = config['lmb']
    census = config['census']
    boost = config['boost']
    gate = config['gate']

    label = f"scout_{config_name}_s{seed}_{log_suffix}" if is_scout else f"finish_{config_name}_s{seed}_{log_suffix}"
    work_dir = os.path.join(root, f"work_{label}")
    if not os.path.exists(work_dir):
        os.makedirs(work_dir)

    scout_log = os.path.join(work_dir, "scout_telemetry.csv")
    
    cmd = [
        vpr_bin, arch, circuit,
        "--route_chan_width", "200",
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
        "--prob_momentum_boost", str(boost)
    ]

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
    
    if os.path.exists(scout_log):
        # Use header parsing for safety
        df = pd.read_csv(scout_log, comment='#', names=['Initial','Final','Gain','Momentum','PBScore'])
        if not df.empty:
            pb_score = df['PBScore'].iloc[0]

    return (seed, config_name, cpd, pb_score, config)

def main():
    CONFIGS = [
        {"name": "Standard-Aggro", "lmb": 0.20, "census": 0.96, "boost": 1.1, "gate": 5e-10},
        {"name": "The-Hammer",     "lmb": 0.50, "census": 0.96, "boost": 1.1, "gate": 5e-10},
        {"name": "The-Scalpel",    "lmb": 0.20, "census": 0.96, "boost": 1.1, "gate": 0.0},
        {"name": "Overdrive",      "lmb": 0.20, "census": 0.96, "boost": 1.5, "gate": 5e-10},
    ]
    
    SCOUT_SUCCESS_TARGET = 0.35 # Physics-based trigger: Stop when acceptance rate < 35%
    SCOUT_LIMIT = 50           # Safety limit (max steps)
    SEEDS = list(range(1, 6))  # 5 seeds per config = 20 scouts total

    print(f"=== Aggresive Portfolio Scouting (4 Configs x 5 Seeds, 45 Cores) ===")
    print(f"Trigger: Acceptance Rate < {SCOUT_SUCCESS_TARGET} (Universal Physics Clock)")

    # 1. Scouting Phase
    print(f"\n[PHASE 1] Launching {len(SEEDS) * len(CONFIGS)} Scouts...")
    scout_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=45) as executor:
        futures = []
        for cfg in CONFIGS:
            for s in SEEDS:
                futures.append(executor.submit(run_vpr, s, cfg, SCOUT_SUCCESS_TARGET, SCOUT_LIMIT, "aggro_scout", True))
        
        for f in concurrent.futures.as_completed(futures):
            scout_results.append(f.result())

    # 2. Selection Phase
    scout_results.sort(key=lambda x: x[3], reverse=True)
    print("\nScouting Rankings (Step 20 - Top 10):")
    print(f"{'Rank':<4} | {'Seed':<5} | {'Config':<15} | {'PB-Score':<10}")
    print("-" * 45)
    for i, (s, name, _, score, _) in enumerate(scout_results[:10]):
        print(f"{i+1:<4} | {s:<5} | {name:<15} | {score:>10.6f}")

    winner = scout_results[0]
    winner_seed, winner_config_name, _, _, winner_config = winner
    print(f"\n🏆 GLOBAL SELECTION: Seed {winner_seed} with Config '{winner_config_name}' chosen for full PNR run.")

    # 3. Finalization Phase
    print(f"\n[PHASE 2] Finishing winner...")
    _, _, final_cpd, _, _ = run_vpr(winner_seed, winner_config, -1, "winner", False)
    
    # Baseline for Winner (approximate)
    BASELINE = 20.225 # Seed 1 baseline for bgm.pre-vpr on k6_N10
    
    print(f"\n=== FINAL RESULT ===")
    print(f"Winner Seed   : {winner_seed}")
    print(f"Winner Config : {winner_config_name}")
    print(f"Final CPD     : {final_cpd:.3f} ns")
    print(f"Net Gain      : {((BASELINE - final_cpd)/BASELINE)*100:.2f}%")

if __name__ == "__main__":
    main()
