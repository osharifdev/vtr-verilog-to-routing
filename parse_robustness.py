import re
import os
import math
from pathlib import Path

OUTPUT_DIR = "sweep_robustness"
BENCHMARKS = ["stereovision2", "diffeq2", "bgm", "blob_merge"]
LAMBDAS = [0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.1]

BASELINES = {
    "stereovision2": 18.62,
    "diffeq2": 18.35,
    "bgm": 18.52,
    "blob_merge": 10.07
}

print("| Lambda | stereovision2 | diffeq2 | bgm | blob_merge | Geom Mean Gain |")
print("| :--- | :--- | :--- | :--- | :--- | :--- |")

for l in LAMBDAS:
    gains = []
    row_cells = [f"**{l}**"]
    
    for b in BENCHMARKS:
        # Construct run directory path
        # Note: Script created dirs like 'stereovision2/stereovision2_l0.005'
        # Or 'stereovision2/stereo_l0.005'?
        # Let's check the run_vpr function: f"{bench}_l{l}"
        run_id = f"{b}_l{l}"
        log_path = Path(OUTPUT_DIR) / b / run_id / "vpr_stdout.log"
        
        cpd = None
        if log_path.exists():
            with open(log_path, "r") as f:
                content = f.read()
                # Search for CPD
                match = re.search(r"Final critical path delay \(least slack\):\s+([0-9.]+)\s+ns", content)
                if match:
                    cpd = float(match.group(1))
        
        if cpd:
            baseline = BASELINES.get(b, cpd)
            gain = (baseline - cpd) / baseline * 100.0
            gains.append(gain)
            row_cells.append(f"{cpd:.2f} ({gain:+.1f}%)")
        else:
            row_cells.append("N/A")
            
    # Calculate Geom Mean of (1 + gain/100)
    # Only if we have 4 results
    if len(gains) == 4:
        product = 1.0
        for g in gains:
            product *= (1 + g/100.0)
        geom_mean_gain = (math.pow(product, 1.0/4.0) - 1.0) * 100.0
        row_cells.append(f"**{geom_mean_gain:+.2f}%**")
    else:
        row_cells.append("")
    
    print("| " + " | ".join(row_cells) + " |")
