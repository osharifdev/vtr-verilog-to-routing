#!/usr/bin/env python3
import os
import sys
import subprocess
import csv
import math
import time
import numpy as np
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--num_seeds", type=int, default=25, help="Number of seeds to run")
parser.add_argument("--benchmark", type=str, default="all", help="Specific benchmark to run, or 'all'")
parser.add_argument("--cores_list", "-c", "--cores", type=str, default="8,16,32", help="Comma-separated list of cores")
parser.add_argument("--timeout", type=int, default=3600*3, help="VPR run timeout in seconds")
parser.add_argument("--clear", action="store_true", help="Clear the benchmark directory before running")
parser.add_argument("--arch", type=str, default="vtr_flow/arch/COFFE_22nm/k6FracN10LB_mem20K_complexDSP_customSB_22nm.xml", help="Path to architecture file")
args = parser.parse_args()

SEEDS = list(range(1, args.num_seeds + 1))
CORES_LIST = [int(x.strip()) for x in args.cores_list.split(",")]

# Koios 2.0 Benchmarks mapped to 6-LUT
ALL_KOIOS_BENCHMARKS = [
    "test",
    "lstm",
    "reduction_layer",
    "conv_layer",
    "eltwise_layer",
    "robot_rl",
    "softmax",
    "tpu_like.small.os",
    "spmv",
    "attention_layer",
    "conv_layer_hls",
    "bwave_like.fixed.small",
    "gemm_layer",
    "bwave_like.fixed.large",
    "dla_like.small",
    "clstm_like.small",
    "bwave_like.float.small",
    "tpu_like.small.ws",
    "clstm_like.medium",
    "tpu_like.large.os",
    "dnnweaver",
    "dla_like.medium",
    "clstm_like.large",
    "bwave_like.float.large",
    "bnn",
    "tpu_like.large.ws",
    "dla_like.large",
    "tdarknet_like.small",
    "tdarknet_like.large",
    "lenet"
]

# Map benchmark name -> Verilog file in koios directory
KOIOS_VERILOG_MAP = {
    "test":                     "test.v",
    "lstm":                     "lstm.v",
    "reduction_layer":          "reduction_layer.v",
    "conv_layer":               "conv_layer.v",
    "conv_layer_hls":           "conv_layer_hls.v",
    "eltwise_layer":            "eltwise_layer.v",
    "robot_rl":                 "robot_rl.v",
    "softmax":                  "softmax.v",
    "tpu_like.small.os":        "tpu_like.small.os.v",
    "spmv":                     "spmv.v",
    "attention_layer":          "attention_layer.v",
    "bwave_like.fixed.small":   "bwave_like.fixed.small.v",
    "gemm_layer":               "gemm_layer.v",
    "bwave_like.fixed.large":   "bwave_like.fixed.large.v",
    "dla_like.small":           "dla_like.small.v",
    "clstm_like.small":         "clstm_like.small.v",
    "bwave_like.float.small":   "bwave_like.float.small.v",
    "tpu_like.small.ws":        "tpu_like.small.ws.v",
    "clstm_like.medium":        "clstm_like.medium.v",
    "tpu_like.large.os":        "tpu_like.large.os.v",
    "dnnweaver":                "dnnweaver.v",
    "dla_like.medium":          "dla_like.medium.v",
    "clstm_like.large":         "clstm_like.large.v",
    "bwave_like.float.large":   "bwave_like.float.large.v",
    "bnn":                      "bnn.v",
    "tpu_like.large.ws":        "tpu_like.large.ws.v",
    "dla_like.large":           "dla_like.large.v",
    "tdarknet_like.small":      "tdarknet_like.small.v",
    "tdarknet_like.large":      "tdarknet_like.large.v",
    "lenet":                    "lenet.v",
}

KOIOS_VERILOG_DIR = "vtr_flow/benchmarks/verilog/koios"
PARMYS_BLIF_DIR   = "koios_parmys_blif"
ARCH_FOR_PARMYS   = "vtr_flow/arch/COFFE_22nm/k6FracN10LB_mem20K_complexDSP_customSB_22nm.xml"

def synthesize_with_parmys(bench):
    """Run Parmys synthesis via run_vtr_flow.py and return the output BLIF path, or None on failure."""
    vfile = KOIOS_VERILOG_MAP.get(bench)
    if not vfile:
        return None
    vpath = os.path.join(KOIOS_VERILOG_DIR, vfile)
    if not os.path.exists(vpath):
        return None

    out_dir = os.path.abspath(os.path.join(PARMYS_BLIF_DIR, bench))
    os.makedirs(out_dir, exist_ok=True)
    blif_out = os.path.join(out_dir, f"{bench}.parmys.blif")
    if os.path.exists(blif_out):
        return blif_out

    print(f"  [INFO] Synthesizing {bench} with Parmys...")

    cmd = [
        sys.executable, "vtr_flow/scripts/run_vtr_flow.py",
        os.path.abspath(vpath),
        os.path.abspath(ARCH_FOR_PARMYS),
        "-temp_dir", out_dir,
        "-start", "parmys",
        "-end", "parmys",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=6000)
        # VTR flow names output BLIF after circuit: <circuit_name>.parmys.blif
        # But it puts it in a subfolder named after the architecture!
        arch_name = os.path.splitext(os.path.basename(ARCH_FOR_PARMYS))[0]
        base = os.path.splitext(vfile)[0]
        candidate = os.path.join(out_dir, arch_name, f"{base}.parmys.blif")
        
        if os.path.exists(candidate):
            import shutil as _sh
            _sh.copy2(candidate, blif_out)
            return blif_out
            
        # Search the whole temp dir for any .blif
        for root, _, files in os.walk(out_dir):
            for f in files:
                if f.endswith(".blif"):
                    fp = os.path.join(root, f)
                    if os.path.samefile(fp, blif_out):
                        return blif_out
                    import shutil as _sh
                    _sh.copy2(fp, blif_out)
                    return blif_out
        print(f"  [WARNING] Parmys finished but no BLIF found for {bench}. stderr: {result.stderr[-500:]}")
    except Exception as e:
        if "are the same file" in str(e):
             return blif_out
        print(f"  [WARNING] Parmys synthesis failed for {bench}: {e}")
    return None


def get_blif_path(bench):
    # Strictly use Parmys-generated BLIFs
    search_paths = [
        f"{PARMYS_BLIF_DIR}/{bench}/{bench}.parmys.blif",
    ]
    for p in search_paths:
        if os.path.exists(p):
            return p
            
    # Fall back to Parmys on-demand synthesis if not found
    return synthesize_with_parmys(bench)

def create_folders():
    base_dir = "VTR_Benchmarks_Koios"
    os.makedirs(base_dir, exist_ok=True)
    for b in ALL_KOIOS_BENCHMARKS:
        os.makedirs(os.path.join(base_dir, b), exist_ok=True)
        
def progress_bar(iteration, total, length=40):
    percent = 100 * (iteration / float(total))
    filled = int(length * iteration // total)
    bar = '=' * filled + '-' * (length - filled)
    sys.stdout.write(f'\r  Progress: |{bar}| {percent:.1f}% ({iteration}/{total} seeds)')
    sys.stdout.flush()
    if iteration == total:
        print()

def main():
    create_folders()
    base_results_dir = "VTR_Benchmarks_Koios"
    
    benchmarks_to_run = ALL_KOIOS_BENCHMARKS if args.benchmark == "all" else [args.benchmark]
    
    for bench_name in benchmarks_to_run:
        if bench_name not in ALL_KOIOS_BENCHMARKS:
            print(f"Warning: {bench_name} not in standard Koios list.")
            
        blif_path = get_blif_path(bench_name)
        if not blif_path:
            print(f"[ERROR] Could not find BLIF file for {bench_name}. Skipping.")
            continue
            
        print(f"\n============================================")
        print(f"Executing Sweep for Koios Benchmark: {bench_name}")
        print(f"BLIF Path: {blif_path}")
        print(f"============================================")
        
        import shutil
        bench_dir = os.path.join(base_results_dir, bench_name)
        if args.clear and os.path.exists(bench_dir):
            print(f"[INFO] Clearing directory {bench_dir}...")
            shutil.rmtree(bench_dir, ignore_errors=True)
            os.makedirs(bench_dir, exist_ok=True)
            
        report_lines = [
            f"# Koios 2.0 Budgeted Portfolio Evaluation - {bench_name}",
            f"- **Benchmark**: {blif_path}",
            f"- **Seeds Run**: {args.num_seeds}",
            "- **Metric**: CPD Mean Reduction (% Difference = (Baseline - Budgeted) / Baseline * 100)",
            "- **Metric**: Latency Overhead (% Difference = (Budgeted - Baseline) / Baseline * 100)",
            ""
        ]
        
        for cores in CORES_LIST:
            print(f"\n-- Configuration: {cores} Cores --")
            results = []
            ran_any = False
            
            for i, seed in enumerate(SEEDS):
                t0 = time.time()
                out_dir = os.path.join(bench_dir, f"eval_{cores}c_seed{seed}")
                csv_path = os.path.join(out_dir, "stage2_full_runs.csv")
                
                if not os.path.exists(csv_path):
                    ran_any = True
                    cmd = [
                        "python3", "run_budgeted_portfolio_singlewave.py",
                        "--blif", blif_path,
                        "--seed", str(seed),
                        "--cores", str(cores),
                        "--out_dir", out_dir,
                        "--timeout", str(args.timeout),
                        "--arch", os.path.abspath(args.arch)
                    ]
                    
                    try:
                        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except subprocess.CalledProcessError:
                        pass
                
                # Parse output
                baseline_cpd = None
                baseline_lat = None
                baseline_wl = None
                baseline_iters = None
                baseline_wmin = None
                best_cpd = None
                best_run_wl = None
                best_run_iters = None
                best_run_wmin = None
                budget_lat = 0.0
                
                if os.path.exists(csv_path):
                    with open(csv_path, 'r') as f:
                        reader = csv.DictReader(f)
                        runs = list(reader)
                        for r in runs:
                            if r['status'] == 'OK' and r['cpd_ns']:
                                cpd = float(r['cpd_ns'])
                                lat = float(r['runtime_s'])
                                wl = float(r['wirelength']) if r.get('wirelength') else None
                                iters = float(r['iterations']) if r.get('iterations') else None
                                wmin = float(r['min_chan_width']) if r.get('min_chan_width') else None
                                
                                if r['type'] == 'baseline':
                                    baseline_cpd = cpd
                                    baseline_lat = lat
                                    baseline_wl = wl
                                    baseline_iters = iters
                                    baseline_wmin = wmin
                                    
                                if best_cpd is None or cpd < best_cpd:
                                    best_cpd = cpd
                                    best_run_wl = wl
                                    best_run_iters = iters
                                    best_run_wmin = wmin
                                    
                            if r['runtime_s']:
                                lat = float(r['runtime_s'])
                                if lat > budget_lat:
                                    budget_lat = lat
                                    
                if baseline_cpd is not None and best_cpd is not None:
                    pct_gain_cpd = (baseline_cpd - best_cpd) / baseline_cpd * 100
                    pct_overhead_lat = (budget_lat - baseline_lat) / baseline_lat * 100 if baseline_lat else 0
                    results.append({
                        "seed": seed,
                        "baseline_cpd": baseline_cpd,
                        "best_cpd": best_cpd,
                        "baseline_wl": baseline_wl,
                        "best_wl": best_run_wl,
                        "baseline_iters": baseline_iters,
                        "best_iters": best_run_iters,
                        "baseline_wmin": baseline_wmin,
                        "best_wmin": best_run_wmin,
                        "pct_gain_cpd": pct_gain_cpd,
                        "baseline_lat": baseline_lat,
                        "budget_lat": budget_lat,
                        "pct_overhead_lat": pct_overhead_lat
                    })
                
                t1 = time.time()
                sys.stdout.write(f"\r  [Seed {seed}] Completed in {t1 - t0:.1f}s".ljust(60) + "\n")
                progress_bar(i + 1, len(SEEDS))

            if not results:
                print("  [ERROR] No valid results produced.")
                continue
                
            mean_base_cpd = np.mean([r['baseline_cpd'] for r in results])
            mean_best_cpd = np.mean([r['best_cpd'] for r in results])
            mean_base_lat = np.mean([r['baseline_lat'] for r in results])
            mean_best_lat = np.mean([r['budget_lat'] for r in results])
            
            valid_base_wl = [r['baseline_wl'] for r in results if r['baseline_wl'] is not None]
            valid_best_wl = [r['best_wl'] for r in results if r['best_wl'] is not None]
            valid_base_iters = [r['baseline_iters'] for r in results if r['baseline_iters'] is not None]
            valid_best_iters = [r['best_iters'] for r in results if r['best_iters'] is not None]
            valid_base_wmin = [r['baseline_wmin'] for r in results if r['baseline_wmin'] is not None]
            valid_best_wmin = [r['best_wmin'] for r in results if r['best_wmin'] is not None]
            
            mean_base_wl = np.mean(valid_base_wl) if valid_base_wl else 0
            mean_best_wl = np.mean(valid_best_wl) if valid_best_wl else 0
            mean_base_iters = np.mean(valid_base_iters) if valid_base_iters else 0
            mean_best_iters = np.mean(valid_best_iters) if valid_best_iters else 0
            mean_base_wmin = np.mean(valid_base_wmin) if valid_base_wmin else 0
            mean_best_wmin = np.mean(valid_best_wmin) if valid_best_wmin else 0
            
            ratios = [r['baseline_cpd'] / r['best_cpd'] for r in results]
            geomean_ratio = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
            geomean_pct_gain = (geomean_ratio - 1) * 100
            mean_pct_diff_lat = ((mean_best_lat - mean_base_lat) / mean_base_lat) * 100 if mean_base_lat else 0
            mean_pct_diff_cpd = ((mean_base_cpd - mean_best_cpd) / mean_base_cpd) * 100 if mean_base_cpd else 0
            mean_pct_diff_wl = ((mean_best_wl - mean_base_wl) / mean_base_wl) * 100 if mean_base_wl else 0
            mean_pct_diff_iters = ((mean_best_iters - mean_base_iters) / mean_base_iters) * 100 if mean_base_iters else 0
            mean_pct_diff_wmin = ((mean_best_wmin - mean_base_wmin) / mean_base_wmin) * 100 if mean_base_wmin else 0
            
            if not ran_any:
                print("  [INFO] Values already exist.")
            print(f"Geometric Mean of % Differences: {geomean_pct_gain:.3f}%")
            print(f"% Difference (Mean CPDs): {mean_pct_diff_cpd:.3f}%")
            print(f"% Difference (Mean Latency): {mean_pct_diff_lat:.2f}%")
            print(f"Mean Baseline Wmin: {mean_base_wmin:.1f} | Mean Best Run Wmin: {mean_best_wmin:.1f} ({mean_pct_diff_wmin:+.2f}%)")
            print(f"Mean Baseline WL: {mean_base_wl:.1f} | Mean Best Run WL: {mean_best_wl:.1f} ({mean_pct_diff_wl:+.2f}%)")
            print(f"Mean Baseline Iters: {mean_base_iters:.2f} | Mean Best Run Iters: {mean_best_iters:.2f} ({mean_pct_diff_iters:+.2f}%)")
            
            report_lines.extend([
                f"### {cores} Cores (Budget: {cores} runs)",
                f"- **Geometric Mean of CPD Gains**: **{geomean_pct_gain:.3f}%**",
                f"- **Mean CPD Reduction**: **{mean_pct_diff_cpd:.3f}%**",
                f"- **Mean Latency Overhead**: **{mean_pct_diff_lat:.2f}%**",
                f"- **Mean Wmin Difference**: {mean_best_wmin:.1f} vs {mean_base_wmin:.1f} (**{mean_pct_diff_wmin:+.2f}%**)",
                f"- **Mean Wirelength Difference**: {mean_best_wl:.1f} vs {mean_base_wl:.1f} (**{mean_pct_diff_wl:+.2f}%**)",
                f"- **Mean Routing Iterations Difference**: {mean_best_iters:.2f} vs {mean_base_iters:.2f} (**{mean_pct_diff_iters:+.2f}%**)",
                ""
            ])
            
        report_path = os.path.join(bench_dir, "summary_report.md")
        with open(report_path, "w") as f:
            f.write("\n".join(report_lines))

if __name__ == "__main__":
    main()
