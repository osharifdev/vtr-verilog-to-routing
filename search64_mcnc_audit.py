#!/usr/bin/env python3
"""
search64_mcnc_audit.py  —  MCNC-20 audit using our VPR build (Search-64 engine)

Usage:
  python3 search64_mcnc_audit.py                      # all 20 benchmarks
  python3 search64_mcnc_audit.py -alu4 -clma -pdc     # subset via flags
  python3 search64_mcnc_audit.py alu4 clma pdc        # subset via positional args
  python3 search64_mcnc_audit.py -seeds 4 -alu4       # fewer baseline seeds
"""
import subprocess, re, os, statistics, argparse
from tqdm import tqdm

VPR  = "./build/vpr/vpr"
ARCH = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
DIR  = "vtr_flow/benchmarks/blif/6"

MCNC_20 = [
    "alu4", "apex2", "apex4", "bigkey", "clma", "des", "diffeq", "dsip",
    "elliptic", "ex1010", "ex5p", "frisc", "misex3", "pdc", "s298",
    "s38417", "s38584.1", "seq", "spla", "tseng"
]

def parse_cpd(text):
    # autonomous winner
    m = re.search(r"WINNER: Worker \d+, CPD=([\d.]+)\s*ns", text)
    if m: return float(m.group(1))
    # standard
    m = re.search(r"Final critical path delay \(least slack\):\s+([\d.]+)\s+ns", text)
    return float(m.group(1)) if m else None

def run(bench, seed=1, autonomous=False):
    blif = f"{DIR}/{bench}.blif"
    cmd  = [VPR, ARCH, blif, "--route_chan_width", "100",
            "--disp", "off", "--seed", str(seed)]
    if autonomous:
        cmd += ["--autonomous", "on"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return parse_cpd(r.stdout + r.stderr)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("-seeds", type=int, default=16)
    p.add_argument("pos_benchmarks", nargs="*", help="Benchmarks via positional args")
    for b in MCNC_20:
        p.add_argument(f"-{b.replace('.','_')}", action="store_true")
    args = p.parse_args()

    # Collect targets from both positional and flags
    targets = []
    # from flags
    for b in MCNC_20:
        if getattr(args, b.replace('.','_'), False):
            targets.append(b)
    # from positional
    for b in args.pos_benchmarks:
        if b in MCNC_20 and b not in targets:
            targets.append(b)
    
    if not targets:
        targets = MCNC_20

    print("=" * 72)
    print(f"MCNC Search-64 Audit  |  {args.seeds} seeds baseline")
    print(f"Benchmarks: {', '.join(targets) if len(targets) <= 5 else str(len(targets)) + ' benchmarks'}")
    print("=" * 72)

    rows = []
    for bench in targets:
        if not os.path.exists(f"{DIR}/{bench}.blif"):
            print(f"  SKIP {bench}: BLIF not found"); continue

        vals = []
        bar = tqdm(range(1, args.seeds+1), desc=f"  {bench:<13}", unit="seed", leave=True)
        for s in bar:
            cpd = run(bench, seed=s)
            if cpd: vals.append(cpd); bar.set_postfix({"cpd": f"{cpd:.4f}"})

        avg  = statistics.mean(vals) if vals else None
        best = min(vals) if vals else None
        tqdm.write(f"  [BASE] {bench:<13}: avg={avg:.4f} ns  best={best:.4f} ns" if avg else f"  [FAIL] {bench}")

        tqdm.write(f"  [S64 ] Running Search-64...")
        auto = run(bench, seed=1, autonomous=True)
        if auto and avg:
            gain = (1 - auto/avg)*100
            tag  = "✅" if gain > 1 else "❌"
            tqdm.write(f"  [S64 ] {bench:<13}: {auto:.4f} ns  gain={gain:+.2f}% {tag}")
        rows.append((bench, avg, best, auto))

    print("\n" + "=" * 72)
    print(f"  {'Benchmark':<14} {'Base Avg':>9} {'Base Best':>10} {'Search-64':>10} {'Gain':>8}")
    print("  " + "-" * 68)
    for b, avg, best, auto in rows:
        if avg and auto:
            gain = (1 - auto/avg)*100
            print(f"  {b:<14} {avg:>9.4f} {best:>10.4f} {auto:>10.4f} {gain:>+7.2f}%")
        elif avg:
            print(f"  {b:<14} {avg:>9.4f} {best:>10.4f} {'FAIL':>10}")
    print("=" * 72)

if __name__ == "__main__":
    main()
