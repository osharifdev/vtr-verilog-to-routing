import re
import os

benchmarks = ['ch_intrinsics', 'raygentop', 'bgm', 'stereovision2']
modes = ['baseline', 'huber_d50']

def extract_stats(filepath):
    stats = {}
    if not os.path.exists(filepath):
        return stats
    with open(filepath, 'r') as f:
        content = f.read()
        
        # Placement wirelength
        m = re.search(r"Total wirelength: ([0-9]+)", content)
        if m: stats['Wirelength'] = int(m.group(1))
            
        m = re.search(r"Final critical path delay \(least slack\):\s+([0-9.]+)\s+ns", content)
        if m: stats['CPD'] = float(m.group(1))
            
        # Placement logic delay
        m = re.search(r"Placement estimated logic delay:\s+([0-9.]+)\s+ns", content)
        if m: stats['Logic Delay'] = float(m.group(1))
            
        # Placement routing delay
        m = re.search(r"Placement estimated routing delay:\s+([0-9.]+)\s+ns", content)
        if m: stats['Routing Delay'] = float(m.group(1))
            
        # Nets and fanout
        m = re.search(r"Nets:\s+([0-9]+)", content)
        if m: stats['Nets'] = int(m.group(1))

    return stats

for b in benchmarks:
    print(f"\n--- {b} ---")
    for m in modes:
        f1 = f"vtr7_comparative_sweep/{b}/{b}_{m}_s1/vpr.out"
        s1 = extract_stats(f1)
        print(f"  {m}: CPD={s1.get('CPD', 0):.3f}ns, Wirelength={s1.get('Wirelength', 0)}, Logic Delay={s1.get('Logic Delay', 0)}, Routing Delay={s1.get('Routing Delay', 0)}")
