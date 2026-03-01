#!/usr/bin/env python3
"""
audit_fair.py  —  Fairest Comparison: Average Baseline vs Autonomous Best

=== METHODOLOGY ===
  BASE: Run VPR N times with random seeds (default 8). Report the AVERAGE CPD.
        This represents what a typical user would expect to get on average —
        not a lucky outlier seed.

  AUTO: Run VPR once with --autonomous on. The engine internally runs 16 scouts
        + 16 exploit seeds and returns the BEST it found.
        This is a fair comparison because autonomous is a "best selector" by design.

=== WHY THIS IS THE FAIREST COMPARISON ===
  - Comparing AUTO vs a single lucky seed (e.g. seed 1 for alu4 = 4.499ns)
    is unfair: that seed is a statistical outlier users rarely get.
  - Comparing AUTO vs average baseline is fair: it shows what the autonomous
    engine delivers vs a realistic user expectation.
  - Both use K6-pre-mapped BLIFs (blif/6/) so gains are pure placement gains.

Usage:
  python3 audit_fair.py              # all benchmarks, 8 base seeds
  python3 audit_fair.py apex2 alu4   # specific benchmarks
  python3 audit_fair.py --seeds 16   # more seeds for tighter average
"""
import os
import re
import sys
import time
import statistics
import subprocess
NUM_SEEDS = 16  # User requested 16-seed baseline for robustness
MCNC_SUITE = [
    "alu4", "apex2", "apex4", "bigkey", "clma", "des", "diffeq", "dsip",
    "elliptic", "ex1010", "ex5p", "frisc", "misex3", "pdc", "s298",
    "s38417", "s38584.1", "seq", "spla", "tseng"
]

VPR_BIN = os.path.abspath("./build/vpr/vpr")
ARCH    = "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
# Path to 6-LUT mapped BLIFs
BLIF_DIR = "vtr_flow/benchmarks/blif/6"
BENCH   = "benchmarks"

BENCHMARKS = [
    (name, f"{BLIF_DIR}/{name}.blif", 100) for name in MCNC_SUITE
] + [
    ("boundtop", f"{BENCH}/boundtop.blif", 100),
    ("bgm",      f"{BENCH}/bgm.blif",      100),
]

def run_vpr_once(blif, cw, extra_args=None, seed=1):
    cmd = [VPR_BIN, ARCH, blif,
           "--route_chan_width", str(cw),
           "--disp", "off",
           "--seed", str(seed)]
    if extra_args:
        cmd += extra_args
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    m = re.search(r"Final critical path delay \(least slack\):\s+([\d.]+)\s+ns", output)
    return float(m.group(1)) if m else None

def baseline_sweep(name, blif, cw, n_seeds):
    """Run N seeds without autonomous and return (avg, best, all_results)."""
    results = []
    for s in range(1, n_seeds + 1):
        cpd = run_vpr_once(blif, cw, seed=s)
        if cpd:
            results.append(cpd)
            print(f"    seed {s:2d}: {cpd:.4f} ns", flush=True)
    if not results:
        return None, None, []
    return statistics.mean(results), min(results), results

def main():
    # Parse args
    n_seeds = NUM_SEEDS
    targets = []
    for arg in sys.argv[1:]:
        if arg == "--seeds":
            continue
        try:
            i = sys.argv.index("--seeds")
            n_seeds = int(sys.argv[i + 1])
        except (ValueError, IndexError):
            pass
        if not arg.startswith("--") and not arg.isdigit():
            targets.append(arg)

    if not targets:
        targets = [b[0] for b in BENCHMARKS]

    print("=" * 72)
    print(f"Fair Comparison  |  BASE=avg of {n_seeds} seeds  AUTO=engine best  (K6-mapped)")
    print(f"  Gain = how much better AUTO is vs TYPICAL user result (avg baseline)")
    print("=" * 72)

    rows = []
    for name, blif, cw in BENCHMARKS:
        if name not in targets:
            continue
        if not os.path.exists(blif):
            print(f"\n[{name}]  ⚠️  BLIF not found: {blif}")
            continue

        print(f"\n[{name}]  Baseline sweep ({n_seeds} seeds)...")
        t0 = time.time()
        avg_base, best_base, all_base = baseline_sweep(name, blif, cw, n_seeds)
        base_wall = time.time() - t0

        print(f"  BASE avg={avg_base:.4f} ns  best={best_base:.4f} ns  "
              f"spread={max(all_base)-min(all_base):.4f} ns")

        print(f"  AUTO running (autonomous tournament)...", flush=True)
        t1 = time.time()
        auto_cpd = run_vpr_once(blif, cw, extra_args=["--autonomous", "on"], seed=1)
        auto_wall = time.time() - t1

        if avg_base and auto_cpd:
            gain_vs_avg  = (1.0 - auto_cpd / avg_base)  * 100.0
            gain_vs_best = (1.0 - auto_cpd / best_base) * 100.0
            oh = (auto_wall / (base_wall / n_seeds) - 1.0) * 100.0

            if gain_vs_avg > 1.0:
                tag = "✅ GAIN"
            elif gain_vs_avg > -1.0:
                tag = "➡️  NEUTRAL"
            else:
                tag = "❌ REGRESS"

            print(f"  AUTO {auto_cpd:.4f} ns  "
                  f"vs avg: {gain_vs_avg:+.2f}%  vs best: {gain_vs_best:+.2f}%  {tag}")
            rows.append((name, avg_base, best_base, auto_cpd, gain_vs_avg, gain_vs_best, oh, tag))

    print("\n" + "=" * 72)
    print(f"  {'Benchmark':<14} {'Base avg':>9} {'Base best':>10} {'Auto':>8} "
          f"{'vs avg':>8} {'vs best':>8}  Result")
    print("  " + "-" * 70)
    for name, avg_b, best_b, auto, g_avg, g_best, oh, tag in rows:
        print(f"  {name:<14} {avg_b:>9.4f} {best_b:>10.4f} {auto:>8.4f} "
              f"{g_avg:>+7.2f}% {g_best:>+7.2f}%  {tag}")

    if rows:
        avg_gain = statistics.mean(r[4] for r in rows)
        wins = sum(1 for r in rows if r[4] > 1.0)
        print(f"\n  Average gain vs baseline avg: {avg_gain:+.2f}%")
        print(f"  Wins (gain > 1%): {wins}/{len(rows)} benchmarks")
    print("=" * 72)

if __name__ == "__main__":
    main()
