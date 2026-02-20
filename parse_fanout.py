import re
import os

benchmarks = ['ch_intrinsics', 'raygentop', 'bgm', 'stereovision2', 'diffeq1', 'mkDelayWorker32B']

def extract_stats(filepath):
    stats = {}
    if not os.path.exists(filepath):
        return stats
    with open(filepath, 'r') as f:
        content = f.read()
        
        # Max fanout
        m = re.search(r"Max Fanout\s*:\s*([0-9]+)", content)
        if m: stats['Max Fanout'] = int(m.group(1))
            
        m = re.search(r"Avg Fanout\s*:\s*([0-9.]+)", content)
        if m: stats['Avg Fanout'] = float(m.group(1))
        
        m = re.search(r"Blocks:\s+([0-9]+)", content)
        if m: stats['Blocks'] = int(m.group(1))
            
        m = re.search(r"Nets:\s+([0-9]+)", content)
        if m: stats['Nets'] = int(m.group(1))
            
    return stats

for b in benchmarks:
    print(f"\n--- {b} ---")
    f1 = f"vtr7_comparative_sweep/{b}/{b}_baseline_s1/vpr.out"
    s1 = extract_stats(f1)
    print(s1)
