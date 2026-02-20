
import os
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm

# --- Configuration ---
VTR_ROOT = "/home/os717/vtr-verilog-to-routing"
RUN_VTR_FLOW = os.path.join(VTR_ROOT, "vtr_flow/scripts/run_vtr_flow.py")
ARCH = os.path.join(VTR_ROOT, "vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml")
BENCH_DIR = os.path.join(VTR_ROOT, "vtr_flow/benchmarks/verilog")

# Official VTR 7.0 Benchmarks (19 Total)
BENCHMARKS = [
    "bgm.v", "blob_merge.v", "boundtop.v", "ch_intrinsics.v", 
    "diffeq1.v", "diffeq2.v", "LU8PEEng.v", "LU32PEEng.v",
    "mcml.v", "mkDelayWorker32B.v", "mkPktMerge.v", "mkSMAdapter4B.v", 
    "or1200.v", "raygentop.v", "sha.v", "stereovision0.v", 
    "stereovision1.v", "stereovision2.v", "stereovision3.v"
]

# Probabilistic Parameters
PROB_FLAGS = [
    "--prob_timing_enable", "on",
    "--prob_timing_strategy", "crit_boost",
    "--prob_timing_alpha", "0.1",
    "--prob_timing_alpha_corr", "0.8",
    "--prob_timing_mc_samples", "1000",
    "--prob_timing_risk_z", "1.645"
]

def run_vpr_task(bench, strategy):
    bench_path = os.path.join(BENCH_DIR, bench)
    name = bench.split('.')[0]
    output_dir = f"run_vtr7_{name}_{strategy}"
    
    cmd = [
        "python3", RUN_VTR_FLOW,
        bench_path,
        ARCH,
        "-start", "odin",
        "-temp_dir", output_dir,
        "-route_chan_width", "100"
    ]
    
    if strategy == "prob":
        cmd += PROB_FLAGS
    
    try:
        # We use check=True to raise exception on failure
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
        
        # Parse WNS from vpr.out
        log_path = os.path.join(output_dir, "vpr.out")
        wns = None
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                for line in f:
                    if "Final setup Worst Negative Slack (sWNS):" in line:
                        parts = line.split(":")[-1].strip().split()
                        if parts:
                            wns = float(parts[0])
        
        if wns is None:
            # Fallback check if vpr_status=success exists in output.txt
            status_path = os.path.join(output_dir, "output.txt")
            if os.path.exists(status_path):
                with open(status_path, "r") as f:
                    if "vpr_status=success" in f.read():
                        # Still couldn't find WNS in log, maybe it's 0 or positive?
                        pass

        return {"bench": name, "strategy": strategy, "wns": wns}
    except subprocess.CalledProcessError as e:
        return {"bench": name, "strategy": strategy, "wns": None, "error": f"Process exited with code {e.returncode}"}
    except Exception as e:
        return {"bench": name, "strategy": strategy, "wns": None, "error": str(e)}

def check_setup():
    """Ensure binaries are available via symlinks."""
    required = ["vpr/vpr", "abc/abc", "odin_ii/odin_ii"]
    missing = []
    for r in required:
        full_path = os.path.join(VTR_ROOT, r)
        if not os.path.exists(full_path):
            missing.append(r)
    
    if missing:
        print(f"ERROR: Missing tool binaries: {missing}")
        print("Please run the following to create symlinks:")
        print(f"  ln -sf ../build/vpr/vpr {os.path.join(VTR_ROOT, 'vpr/vpr')}")
        print(f"  ln -sf ../build/abc/abc {os.path.join(VTR_ROOT, 'abc/abc')}")
        print(f"  ln -sf ../build/odin_ii/odin_ii {os.path.join(VTR_ROOT, 'odin_ii/odin_ii')}")
        return False
    return True

def main():
    if not check_setup():
        return

    print(f"=== PARALLEL VTR 7.0 AUDIT (Baseline vs Probabilistic) ===")
    print(f"Total Benchmarks: {len(BENCHMARKS)}")
    print(f"Strategies: Baseline, Crit_Boost")
    print("-" * 50)

    results = {}
    tasks = []
    
    # We use ProcessPoolExecutor for true parallelism
    max_workers = min(os.cpu_count(), 8)
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for bench in BENCHMARKS:
            tasks.append(executor.submit(run_vpr_task, bench, "baseline"))
            tasks.append(executor.submit(run_vpr_task, bench, "prob"))
        
        with tqdm(total=len(tasks), desc="Auditing VTR 7.0") as pbar:
            for future in as_completed(tasks):
                res = future.result()
                bench = res["bench"]
                if bench not in results: results[bench] = {}
                results[bench][res["strategy"]] = res["wns"]
                pbar.update(1)

    # Final Report
    print("\n" + "="*60)
    print(f"{'Benchmark':<20} | {'Baseline':>10} | {'Prob':>10} | {'% Gain':>10}")
    print("-"*60)
    
    for bench in sorted(results.keys()):
        base = results[bench].get("baseline")
        prob = results[bench].get("prob")
        
        if base is not None and prob is not None:
            diff = prob - base
            pct = (diff / abs(base)) * 100.0 if base != 0 else 0
            print(f"{bench:<20} | {base:10.4f} | {prob:10.4f} | {pct:+10.2f}%")
        else:
            print(f"{bench:<20} | {'FAIL':>10} | {'FAIL':>10} | {'N/A':>10}")
    print("="*60)

if __name__ == "__main__":
    main()
