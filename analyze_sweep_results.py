import os
import re
import glob

def get_cpd(log_file):
    if not os.path.exists(log_file): return None
    with open(log_file, "r") as f:
        content = f.read()
        if "Final critical path delay" in content:
            m = re.search(r"Final critical path delay.*: ([\d\.]+) ns", content)
            return float(m.group(1))
        if "Routing failed" in content:
            return "Fail_Route"
        if "The entire flow of VPR took" in content and "VPR succeeded" not in content:
             if "Router: SKIP" in content: return "Skip_Route"
             return "Fail_Unknown"
    return "Crash"

def analyze_bench(bench, seeds):
    print(f"=== Analysis for {bench} ===")
    baselines = {}
    
    # 1. Get Baselines
    for seed in seeds:
        log = f"sweep_{bench}_seed{seed}_base/vpr.log"
        cpd = get_cpd(log)
        if isinstance(cpd, float):
            baselines[seed] = cpd
            print(f"  Seed {seed} Base: {cpd:.4f} ns")
        else:
            print(f"  Seed {seed} Base: FAILED ({cpd})")
            
    # 2. Get Prob Results
    results = []
    prob_dirs = glob.glob(f"sweep_{bench}_seed*_prob_*")
    
    for d in prob_dirs:
        m = re.match(r".*seed(\d+)_prob_L([\d\.]+)_A([\d\.]+)", d)
        if not m: continue
        seed = int(m.group(1))
        lam = float(m.group(2))
        alpha = float(m.group(3))
        
        log = os.path.join(d, "vpr.log")
        cpd = get_cpd(log)
        
        if isinstance(cpd, float) and seed in baselines:
            base = baselines[seed]
            gain = (base - cpd) / base * 100
            results.append((seed, lam, alpha, gain))
        elif seed in baselines:
             # print(f"    Seed {seed} L={lam} A={alpha} -> {cpd}")
             pass

    # 3. Aggregate
    if not results:
        print("  No successful probabilistic runs.")
        return

    # Group by (L, A)
    configs = {}
    for s, l, a, g in results:
        k = (l, a)
        if k not in configs: configs[k] = []
        configs[k].append(g)
        
    # Sort by avg gain
    avgs = []
    for k, gains in configs.items():
        avg = sum(gains) / len(gains)
        avgs.append((k, avg, gains))
        
    avgs.sort(key=lambda x: x[1], reverse=True)
    
    print("\n  Top Configurations:")
    for (l, a), avg, gains in avgs[:5]:
        gains_str = ", ".join([f"{g:+.2f}%" for g in gains])
        print(f"    L={l} A={a} -> Avg: {avg:+.2f}% (Gains: {gains_str})")

seeds = [1, 4, 13]
analyze_bench("stereovision2", seeds)
analyze_bench("blob_merge", seeds)
