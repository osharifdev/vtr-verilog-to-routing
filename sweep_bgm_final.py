import subprocess
import os
import re
import concurrent.futures

def run_vpr(seed, adaptive=True):
    root = "/home/os717/vtr-verilog-to-routing"
    vpr_bin = os.path.join(root, "build/vpr/vpr")
    arch = os.path.join(root, "vtr_flow/arch/timing/k6_N10_mem32K_40nm.xml")
    circuit = os.path.join(root, "phase4_bgm/bgm_baseline_s1/bgm.pre-vpr.blif")

    cmd = [
        vpr_bin, arch, circuit,
        "--route_chan_width", "200",
        "--seed", str(seed),
        "--timing_report_detail", "aggregated",
        "--place_quench_only", "on"
    ]

    if adaptive:
        cmd += [
            "--prob_timing_enable", "on",
            "--prob_timing_inject", "on",
            "--prob_timing_mode", "4",
            "--prob_inject_mode", "regularize",
            "--prob_self_calibrate", "on",
            "--prob_schedule_ramp", "on",
            "--prob_slack_gate", "1e-9", # 1.0ns
            "--prob_congestion_gamma", "0.5"
        ]
    
    label = "adaptive" if adaptive else "baseline"
    log_file = os.path.join(root, f"bgm_final_s{seed}_{label}.log")
    
    with open(log_file, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    
    cpd = None
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            for line in f:
                if "post-quench CPD =" in line:
                    match = re.search(r"post-quench CPD = ([\d\.]+) \(ns\)", line)
                    if match:
                        cpd = float(match.group(1))
                        break
    return seed, adaptive, cpd

def main():
    seeds = [1, 2, 3, 4, 5]
    results = []
    
    print("=== Phase 7.1 Stability v2: Final 5-Seed Clean Verification (BGM) ===")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for seed in seeds:
            futures.append(executor.submit(run_vpr, seed, False)) # Baseline
            futures.append(executor.submit(run_vpr, seed, True))  # Adaptive
        
        for future in concurrent.futures.as_completed(futures):
            seed, adaptive, cpd = future.result()
            label = "Adaptive" if adaptive else "Baseline"
            print(f"  --> Seed {seed} {label}: {cpd} ns")
            results.append((seed, adaptive, cpd))

    # Summary
    print("\n--- Summary Results ---")
    print("| Seed | Baseline CPD | Adaptive CPD | Gain (%) |")
    print("|------|--------------|--------------|----------|")
    
    seeded_results = {}
    for seed, adaptive, cpd in results:
        if seed not in seeded_results: seeded_results[seed] = {}
        seeded_results[seed][adaptive] = cpd

    mean_base = 0
    mean_auto = 0
    count = 0

    for seed in sorted(seeded_results.keys()):
        base = seeded_results[seed].get(False)
        auto = seeded_results[seed].get(True)
        if base and auto:
            gain = (base - auto) / base * 100
            print(f"| {seed:^4} | {base:^12.3f} | {auto:^12.3f} | {gain:^+8.2f} |")
            mean_base += base
            mean_auto += auto
            count += 1
    
    if count > 0:
        mean_base /= count
        mean_auto /= count
        total_gain = (mean_base - mean_auto) / mean_base * 100
        print(f"|{'MEAN':^6}| {mean_base:^12.3f} | {mean_auto:^12.3f} | {total_gain:^+8.2f} |")

if __name__ == "__main__":
    main()
