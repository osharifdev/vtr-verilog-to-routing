import re
import os
import statistics
from pathlib import Path

OUTPUT_DIR = "verify_multiseed"
BENCHMARKS = ["stereovision2", "diffeq2", "bgm", "blob_merge"]
SEEDS = [1, 2, 3, 4, 5]

print("## Multi-Seed Verification (L=0.02)")
print("Testing robust configuration on 5 random seeds per benchmark.\n")

for b in BENCHMARKS:
    print(f"### {b}")
    print("| Seed | Baseline (ns) | Quadratic (ns) | Gain |")
    print("| :--- | :--- | :--- | :--- |")
    
    gains = []
    
    for s in SEEDS:
        # Construct paths
        # Format: {bench}_s{seed}_{config}
        base_id = f"{b}_s{s}_baseline"
        exp_id = f"{b}_s{s}_experiment"
        
        base_path = Path(OUTPUT_DIR) / b / base_id / "vpr_stdout.log"
        exp_path = Path(OUTPUT_DIR) / b / exp_id / "vpr_stdout.log"
        
        base_cpd = None
        exp_cpd = None
        
        if base_path.exists():
            with open(base_path, "r") as f:
                content = f.read()
                # Try strict first, then loose
                m = re.search(r"Final critical path delay.*:\s+([0-9.]+)\s+ns", content)
                if m: base_cpd = float(m.group(1))
                else: print(f"DEBUG: {base_id} file exists but regex failed. Tail: {content[-100:]}")
        else:
             print(f"DEBUG: {base_id} file NOT found at {base_path}")
        
        if exp_path.exists():
            with open(exp_path, "r") as f:
                content = f.read()
                m = re.search(r"Final critical path delay.*:\s+([0-9.]+)\s+ns", content)
                if m: exp_cpd = float(m.group(1))
                
        if base_cpd and exp_cpd:
            gain = (base_cpd - exp_cpd) / base_cpd * 100.0
            gains.append(gain)
            gain_str = f"**{gain:+.1f}%**" if gain > 0 else f"{gain:+.1f}%"
            print(f"| **{s}** | {base_cpd:.2f} | {exp_cpd:.2f} | {gain_str} |")
        else:
            print(f"| **{s}** | {base_cpd} | {exp_cpd} | N/A |")
            
    if gains:
        avg_gain = statistics.mean(gains)
        print(f"**Avg Gain**: {avg_gain:+.2f}%\n")
    else:
        print("**Avg Gain**: N/A\n")
