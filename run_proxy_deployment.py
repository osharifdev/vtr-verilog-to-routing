#!/usr/bin/env python3
"""
Complete proxy deployment analysis - runs full evals if needed, collects proxy data, then analyzes.

Usage:
    python3 run_proxy_deployment.py --benchmark test --seeds 10 --cores 1,2,4,8,12,16
    python3 run_proxy_deployment.py --benchmark test --seeds 10 --cores 1,2,4,8,12,16 --clear
"""

import sys
import argparse
import csv
import concurrent.futures
import json
import math
import shutil
import subprocess
import time
import os
from pathlib import Path
from statistics import geometric_mean, mean, stdev, NormalDist

def pick_free_cores(n):
    """Return a list of n core IDs with the lowest CPU utilisation from /proc/stat."""
    def read_cpu_times():
        times = {}
        with open('/proc/stat') as f:
            for line in f:
                if not line.startswith('cpu') or line.startswith('cpu '):
                    continue
                parts = line.split()
                core_id = int(parts[0][3:])
                idle    = int(parts[4])
                total   = sum(int(x) for x in parts[1:])
                times[core_id] = (idle, total)
        return times

    t1 = read_cpu_times()
    time.sleep(0.2)
    t2 = read_cpu_times()

    usage = {}
    for cid in t1:
        d_idle  = t2[cid][0] - t1[cid][0]
        d_total = t2[cid][1] - t1[cid][1]
        usage[cid] = 0.0 if d_total == 0 else 1.0 - d_idle / d_total

    ranked = sorted(usage, key=lambda c: usage[c])
    selected = ranked[:n]
    return sorted(selected)

def progress_bar(current, total, width=40):
    """Generate a progress bar string."""
    filled = int(width * current / total)
    bar = '=' * filled + ' ' * (width - filled)
    pct = 100.0 * current / total
    return f"|{bar}| {pct:5.1f}% ({current}/{total})"

ABLATION_DIR = {'full': 'eval_48c', 'v0_only': 'eval_v0only', 'pqt_only': 'eval_pqtonly'}
ABLATION_LABEL = {'full': 'V0+PQT (Full)', 'v0_only': 'V0 Only', 'pqt_only': 'PQT Only',
                  'v0_compete': 'V0 (B.Competes)', 'v0_forced': 'V0 (B.Forced)',
                  'pqt_compete': 'PQT (B.Competes)', 'pqt_forced': 'PQT (B.Forced)'}
ABLATION_CORES = {'full': 48, 'v0_only': 3, 'pqt_only': 16}

RESULTS_DIR = "PAPER_RESULTS_KOIOS"  # overridden by --results_dir at startup

def eval_dir_path(benchmark, seed, ablation):
    return Path(f"{RESULTS_DIR}/{benchmark}/{ABLATION_DIR[ablation]}_seed{seed}")

def run_full_evaluation(benchmark, seed, ablation='full', timeout=3600*3, max_workers=0,
                        pinned_cores=None, indent='    '):
    """Run evaluation for a seed using run_budgeted_portfolio_singlewave.py"""
    ARCH = "vtr_flow/arch/COFFE_22nm/k6FracN10LB_mem20K_complexDSP_customSB_22nm.xml"
    BLIF = f"koios_parmys_blif/{benchmark}/{benchmark}.parmys.blif"

    out_dir = eval_dir_path(benchmark, seed, ablation)

    cmd = [
        "python3", "run_budgeted_portfolio_singlewave.py",
        "--blif", os.path.abspath(BLIF),
        "--seed", str(seed),
        "--cores", str(ABLATION_CORES[ablation]),
        "--out_dir", os.path.abspath(out_dir),
        "--timeout", str(timeout),
        "--arch", os.path.abspath(ARCH),
        "--ablation", ablation,
        "--quiet"
    ]
    if max_workers > 0:
        cmd += ["--max_workers", str(max_workers)]
    if pinned_cores:
        cmd += ["--cpu_list", ",".join(str(c) for c in pinned_cores)]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            print(f"{indent}{line}", end='', flush=True)
        proc.wait()
        return proc.returncode == 0
    except Exception:
        return False

def load_candidate_meta(benchmark, seed, ablation):
    """Load portfolio_candidates.csv for a seed/ablation, returning {cid: meta_dict} excluding C0."""
    cand_csv = eval_dir_path(benchmark, seed, ablation) / "portfolio_candidates.csv"
    meta = {}
    with open(cand_csv) as f:
        for row in csv.DictReader(f):
            if row['candidate_id'] == 'C0':
                continue
            meta[row['candidate_id']] = row  # keys: type, v0_cfg, pqt_template, lambda, alpha, beta, qstart, qend
    return meta

def collect_proxy_data(benchmark, seed_range, ablation='full', clear=False, max_workers=0, pinned_cores=None):
    """Collect proxy checkpoint data for all seeds of a given ablation if not already present."""

    ARCH = "vtr_flow/arch/COFFE_22nm/k6FracN10LB_mem20K_complexDSP_customSB_22nm.xml"
    BLIF = f"koios_parmys_blif/{benchmark}/{benchmark}.parmys.blif"
    VPR = "build/vpr/vpr"

    OUTPUT_DIR = Path(f"{RESULTS_DIR}/{benchmark}/proxy_checkpoints_cow")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    suffix = '' if ablation == 'full' else f'_{ablation}'
    master_file = OUTPUT_DIR / f"proxy_master{suffix}.csv"

    if clear and master_file.exists():
        print(f"\n✗ Deleting existing proxy data: {master_file}", flush=True)
        master_file.unlink()

    # Check which seeds have eval data available for this ablation
    available_seeds = []
    for seed in range(*seed_range):
        eval_csv = eval_dir_path(benchmark, seed, ablation) / "stage2_full_runs.csv"
        if eval_csv.exists():
            available_seeds.append(seed)

    if not available_seeds:
        print(f"\n✗ No {ablation} eval data found for any seeds in range {seed_range[0]}-{seed_range[1]-1}\n", flush=True)
        return

    # Track at (seed, config) level so partial seeds get topped up
    existing_pairs = set()
    all_proxy_data = []
    if master_file.exists():
        with open(master_file) as f:
            all_proxy_data = list(csv.DictReader(f))
        for row in all_proxy_data:
            existing_pairs.add((int(row['seed']), row['config_id']))

    # A seed needs collection if any config is missing
    seeds_to_collect = []
    for seed in available_seeds:
        cand_ids = list(load_candidate_meta(benchmark, seed, ablation).keys())
        missing = [c for c in cand_ids if (seed, c) not in existing_pairs]
        if missing:
            seeds_to_collect.append(seed)

    if not seeds_to_collect:
        print(f"\n✓ [{ABLATION_LABEL[ablation]}] Proxy data already exists for all {len(available_seeds)} seeds\n", flush=True)
        return

    print(f"\n{'='*70}", flush=True)
    print(f"COLLECTING PROXY CHECKPOINTS — {ABLATION_LABEL[ablation]}", flush=True)
    print(f"Benchmark: {benchmark} | Seeds to collect: {seeds_to_collect}", flush=True)
    print(f"{'='*70}\n", flush=True)

    def build_proxy_cmd(seed, cand_meta, output_csv):
        """Build VPR proxy command from candidate metadata."""
        vpr_abs  = os.path.abspath(VPR)
        arch_abs = os.path.abspath(ARCH)
        blif_abs = os.path.abspath(BLIF)
        base = [vpr_abs, arch_abs, blif_abs, "--seed", str(seed), "--disp", "off",
                "--proxy_checkpoint_enable", "on",
                "--proxy_checkpoint_output", str(output_csv),
                "--proxy_checkpoint_stop_after", "3"]
        ctype = cand_meta['type']
        if ctype == 'v0_only':
            return base + [
                "--v0_enable", "on", "--v0_macro_mode", "1", "--v0_ordering_mode", "stable",
                "--prob_config_id", str(cand_meta['v0_cfg']),
                "--prob_timing_enable", "off", "--prob_timing_inject", "off"
            ]
        elif ctype == 'pqt_only':
            return base + [
                "--v0_enable", "off", "--prob_config_id", "0",
                "--prob_timing_inject", "on", "--prob_timing_enable", "on", "--prob_timing_mode", "4",
                "--prob_inject_mode", "quantile",
                "--prob_inject_lambda", str(cand_meta['lambda']),
                "--prob_timing_alpha", str(cand_meta['alpha']),
                "--prob_timing_beta", str(cand_meta['beta']),
                "--prob_inject_quantile_start", str(cand_meta['qstart']),
                "--prob_inject_quantile_end", str(cand_meta['qend'])
            ]
        else:  # joint_pair
            return base + [
                "--v0_enable", "on", "--v0_macro_mode", "1", "--v0_ordering_mode", "stable",
                "--prob_config_id", str(cand_meta['v0_cfg']),
                "--prob_timing_inject", "on", "--prob_timing_enable", "on", "--prob_timing_mode", "4",
                "--prob_inject_mode", "quantile",
                "--prob_inject_lambda", str(cand_meta['lambda']),
                "--prob_timing_alpha", str(cand_meta['alpha']),
                "--prob_timing_beta", str(cand_meta['beta']),
                "--prob_inject_quantile_start", str(cand_meta['qstart']),
                "--prob_inject_quantile_end", str(cand_meta['qend'])
            ]

    def run_one_proxy(seed, config_id, cand_meta):
        import tempfile, shutil
        core_id = core_pool.get() if pinned_cores else None
        tmpdir = Path(tempfile.mkdtemp(prefix=f"proxy_{ablation}_{seed}_{config_id}_"))
        output_csv = tmpdir / "proxy_out.csv"
        try:
            cmd = build_proxy_cmd(seed, cand_meta, output_csv)
            if core_id is not None:
                cmd = ["taskset", "-c", str(core_id)] + cmd
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmpdir))
            if result.returncode == 0 and output_csv.exists():
                with open(output_csv) as f:
                    for row in csv.DictReader(f):
                        if row['iteration'] == '0':
                            row['benchmark'] = benchmark
                            row['seed'] = str(seed)
                            row['config_id'] = config_id
                            return row
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            if pinned_cores and core_id is not None:
                core_pool.put(core_id)
        return None

    workers = max_workers if max_workers > 0 else 48
    seed_proxy_times = {}

    # Build a pool of core IDs to assign one-per-concurrent-VPR-process
    import queue as _queue
    core_pool = _queue.Queue()
    if pinned_cores:
        for _c in pinned_cores[:workers]:
            core_pool.put(_c)

    for seed_idx, seed in enumerate(seeds_to_collect, 1):
        cand_meta_map = load_candidate_meta(benchmark, seed, ablation)
        config_ids = [c for c in cand_meta_map if (seed, c) not in existing_pairs]
        total_configs = len(config_ids)

        n_waves = math.ceil(total_configs / workers)
        print(f"[Seed {seed:2d}] {ABLATION_LABEL[ablation]} Proxy | {total_configs} configs | {workers} workers | {n_waves} wave(s)", flush=True)
        start_time = time.time()

        enumerated = list(enumerate(config_ids, 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for wave_idx in range(n_waves):
                wave = enumerated[wave_idx * workers : (wave_idx + 1) * workers]
                wave_t0 = time.time()
                print(f"  [Wave {wave_idx+1}/{n_waves}] Running {len(wave)} proxy configs...", end=" ", flush=True)
                wave_futures = [executor.submit(run_one_proxy, seed, cfg, cand_meta_map[cfg])
                                for _, cfg in wave]
                for future in concurrent.futures.as_completed(wave_futures):
                    row = future.result()
                    if row is not None:
                        all_proxy_data.append(row)
                done = min((wave_idx + 1) * workers, total_configs)
                print(f"Complete | {done}/{total_configs} configs | Time: {time.time()-wave_t0:.1f}s", flush=True)

        elapsed = time.time() - start_time
        # Store single-proxy latency (time for one VPR proxy run = one wave / configs per wave)
        t_one = elapsed / n_waves
        seed_proxy_times[seed] = t_one
        print(f"  Seed {seed:2d} proxy complete in {elapsed:.1f}s total | t_one={t_one:.2f}s", flush=True)
        print(f"  Progress: {progress_bar(seed_idx, len(seeds_to_collect))}", flush=True)
        print(flush=True)

        # Persist incrementally after each seed so Ctrl+C doesn't lose work
        if all_proxy_data:
            fieldnames = ['benchmark', 'seed', 'config_id'] + [k for k in all_proxy_data[0].keys()
                                                                 if k not in ['benchmark', 'seed', 'config_id']]
            with open(master_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_proxy_data)

        # Persist proxy timing incrementally too
        import json
        timing_file = OUTPUT_DIR / f"proxy_timing{suffix}.json"
        existing_times = {}
        if timing_file.exists():
            with open(timing_file) as f:
                existing_times = {int(k): v for k, v in json.load(f).items()}
        existing_times.update(seed_proxy_times)
        with open(timing_file, 'w') as f:
            json.dump(existing_times, f, indent=2)

    print(f"✓ [{ABLATION_LABEL[ablation]}] Proxy collection complete: {len(all_proxy_data)} total rows\n", flush=True)

def collect_c0_proxy_data(benchmark, seeds, arch=None, max_workers=8):
    """Run VPR proxy for C0 (baseline, no V0/PQT) per seed. Returns {seed: {net_bb_std, net_bb_p90}}.
    Results are cached to proxy_checkpoints_cow/proxy_c0_baseline.json."""
    ARCH = arch or "vtr_flow/arch/timing/k6_frac_N10_frac_chain_mem32K_40nm.xml"
    blif_paths = [
        f"koios_parmys_blif/{benchmark}/{benchmark}.parmys.blif",
        f"{RESULTS_DIR}/{benchmark}/{benchmark}.blif",
        f"vtr_flow/benchmarks/koios/{benchmark}.v",
    ]
    BLIF = next((p for p in blif_paths if os.path.exists(p)), None)
    if not BLIF:
        return {}

    cache_path = Path(f"{RESULTS_DIR}/{benchmark}/proxy_checkpoints_cow/proxy_c0_baseline.json")
    cached = {}
    if cache_path.exists():
        with open(cache_path) as f:
            cached = {int(k): v for k, v in json.load(f).items()}

    missing = [s for s in seeds if s not in cached]
    if not missing:
        return cached

    print(f"  [C0 Proxy] Collecting baseline proxy metrics for {len(missing)} seed(s)...", flush=True)

    def run_one(seed):
        import tempfile
        tmpdir     = Path(tempfile.mkdtemp(prefix=f"proxy_c0_{benchmark}_{seed}_"))
        output_csv = tmpdir / "proxy_out.csv"
        try:
            cmd = [
                os.path.abspath("build/vpr/vpr"),
                os.path.abspath(ARCH),
                os.path.abspath(BLIF),
                "--seed", str(seed), "--disp", "off",
                "--proxy_checkpoint_enable", "on",
                "--proxy_checkpoint_output", str(output_csv),
                "--proxy_checkpoint_stop_after", "3",
                "--v0_enable", "off",
                "--prob_timing_enable", "off",
                "--prob_timing_inject", "off",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmpdir))
            if result.returncode == 0 and output_csv.exists():
                with open(output_csv) as f:
                    for row in csv.DictReader(f):
                        if row['iteration'] == '0':
                            return seed, {'net_bb_std': float(row['net_bb_std']),
                                          'net_bb_p90': float(row['net_bb_p90'])}
        except Exception:
            pass
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return seed, None

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(run_one, s): s for s in missing}
        for fut in concurrent.futures.as_completed(futs):
            seed, metrics = fut.result()
            if metrics:
                results[seed] = metrics
                print(f"    Seed {seed}: net_bb_std={metrics['net_bb_std']:.4f}  net_bb_p90={metrics['net_bb_p90']:.4f}", flush=True)
            else:
                print(f"    Seed {seed}: FAILED", flush=True)

    cached.update(results)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, 'w') as f:
        json.dump({str(k): v for k, v in cached.items()}, f, indent=2)
    return cached

def load_proxy_data(benchmark, seed, ablation='full'):
    suffix = '' if ablation == 'full' else f'_{ablation}'
    master_file = f"{RESULTS_DIR}/{benchmark}/proxy_checkpoints_cow/proxy_master{suffix}.csv"
    configs = {}
    with open(master_file) as f:
        for row in csv.DictReader(f):
            if row['iteration'] == '0' and int(row['seed']) == seed:
                cfg = row['config_id']
                configs[cfg] = {
                    'net_bb_std': float(row['net_bb_std']),
                    'net_bb_p90': float(row['net_bb_p90']),
                }
    return configs

def load_proxy_timing(benchmark, ablation='full'):
    """Load measured proxy collection times {seed: elapsed_s}. Returns empty dict if not found."""
    import json
    suffix      = '' if ablation == 'full' else f'_{ablation}'
    timing_file = Path(f"{RESULTS_DIR}/{benchmark}/proxy_checkpoints_cow/proxy_timing{suffix}.json")
    if not timing_file.exists():
        return {}
    with open(timing_file) as f:
        return {int(k): v for k, v in json.load(f).items()}

def load_full_run_data(benchmark, seed, ablation='full'):
    full_csv = eval_dir_path(benchmark, seed, ablation) / "stage2_full_runs.csv"
    configs = {}
    with open(full_csv) as f:
        for row in csv.DictReader(f):
            cfg = row['candidate_id']
            if not row.get('cpd_ns') or not row.get('runtime_s'):
                continue  # skip rows with empty metrics
            try:
                configs[cfg] = {
                    'cpd': float(row['cpd_ns']),
                    'wirelength': int(row['wirelength']),
                    'iterations': int(row['iterations']),
                    'runtime': float(row['runtime_s'])
                }
            except (ValueError, KeyError):
                continue  # skip malformed rows
    return configs

def select_top_n_interleaved(proxy_data, n):
    configs = list(proxy_data.items())
    ranked_std = sorted(configs, key=lambda x: x[1]['net_bb_std'])
    ranked_p90 = sorted(configs, key=lambda x: x[1]['net_bb_p90'])

    selected = set()
    i = 0
    while len(selected) < n and i < len(configs) * 2:
        if i % 2 == 0 and i // 2 < len(ranked_std):
            selected.add(ranked_std[i // 2][0])
        elif i % 2 == 1 and i // 2 < len(ranked_p90):
            selected.add(ranked_p90[i // 2][0])
        i += 1

    return list(selected)[:n]

def analyze_seed(full_run_data, proxy_data, num_cores, proxy_time, canonical_baseline=None,
                 strategy='normal'):
    # C0 is required as the baseline — skip seeds where C0 failed
    if 'C0' not in full_run_data:
        return None
    if strategy == 'no_c0':
        # Pure proxy selection — no C0 appended; shows true regression rate
        selected_configs  = [c for c in select_top_n_interleaved(proxy_data, num_cores)
                             if c in full_run_data]
        candidate_configs = selected_configs
    elif strategy == 'forced':
        # top-(N-1) from proxy + C0 always gets the Nth slot
        n_proxy = max(0, num_cores - 1)
        proxy_selected = [c for c in select_top_n_interleaved(proxy_data, n_proxy)
                          if c != 'C0' and c in full_run_data] if n_proxy > 0 else []
        selected_configs  = proxy_selected + ['C0']
        candidate_configs = selected_configs
    else:
        # normal: proxy selects top-N (augmented pool may include C0); C0 appended if not chosen
        selected_configs  = [c for c in select_top_n_interleaved(proxy_data, num_cores)
                             if c in full_run_data]
        candidate_configs = selected_configs if 'C0' in selected_configs \
                            else list(selected_configs) + ['C0']

    candidate_configs = [c for c in candidate_configs if c in full_run_data]
    if not candidate_configs:
        raise ValueError("No valid evaluated configs (all may have failed)")
    portfolio_cpd = min([full_run_data[c]['cpd'] for c in candidate_configs])
    portfolio_config = min(candidate_configs, key=lambda c: full_run_data[c]['cpd'])
    portfolio_stats = full_run_data[portfolio_config]

    baseline_stats = full_run_data['C0']

    oracle_best_cpd = min([full_run_data[c]['cpd'] for c in full_run_data])
    oracle_best_config = min(full_run_data.keys(), key=lambda c: full_run_data[c]['cpd'])

    cpd_gain_pct = ((baseline_stats['cpd'] - portfolio_cpd) / baseline_stats['cpd']) * 100
    mean_regret = portfolio_cpd - oracle_best_cpd
    containment = 1 if portfolio_config == oracle_best_config else 0

    proxy_wallclock = proxy_time
    # C0 was already run as the baseline — exclude it from selected runtime cost
    non_c0_selected  = [c for c in selected_configs if c != 'C0']
    slowest_selected = max(full_run_data[c]['runtime'] for c in non_c0_selected) \
                       if non_c0_selected else 0.0
    portfolio_wallclock = proxy_wallclock + slowest_selected
    # Always use the canonical C0 runtime from eval_48c (full run) as the wall-clock baseline.
    # Ablation runs (v0_only, pqt_only) have their own C0 which varies due to machine load,
    # making cross-ablation comparison invalid if we use each ablation's own C0.
    baseline_wallclock = canonical_baseline['runtime'] if canonical_baseline else baseline_stats['runtime']
    wall_clk_pct  = ((portfolio_wallclock / baseline_wallclock) - 1) * 100
    proxy_pct     = (proxy_wallclock / baseline_wallclock) * 100          # proxy overhead as % of baseline
    run_pct       = ((slowest_selected / baseline_wallclock) - 1) * 100   # run overhead as % of baseline

    return {
        'selected_configs': selected_configs,
        'portfolio_config': portfolio_config,
        'portfolio_cpd': portfolio_cpd,
        'baseline_cpd': baseline_stats['cpd'],
        'oracle_best_cpd': oracle_best_cpd,
        'oracle_best_config': oracle_best_config,
        'cpd_gain_pct': cpd_gain_pct,
        'mean_regret': mean_regret,
        'containment': containment,
        'portfolio_wirelength': portfolio_stats['wirelength'],
        'baseline_wirelength': baseline_stats['wirelength'],
        'portfolio_iterations': portfolio_stats['iterations'],
        'baseline_iterations': baseline_stats['iterations'],
        'proxy_wallclock': proxy_wallclock,
        'portfolio_wallclock': portfolio_wallclock,
        'baseline_wallclock': baseline_wallclock,
        'wall_clk_pct': wall_clk_pct,
        'proxy_pct': proxy_pct,
        'run_pct': run_pct,
    }

def print_summary_report(benchmark, num_cores, num_seeds, results, proxy_time):
    containment_total = sum(r['containment'] for r in results)
    containment_pct = (containment_total / num_seeds) * 100
    mean_regret_avg = mean(r['mean_regret'] for r in results)

    cpd_gains = [r['cpd_gain_pct'] for r in results]
    cpd_gain_geomean = geometric_mean([1 + g/100 for g in cpd_gains]) * 100 - 100
    cpd_gain_mean = mean(cpd_gains)
    wins = sum(1 for g in cpd_gains if g > 0)
    regressions = sum(1 for g in cpd_gains if g < 0)

    baseline_cpd_mean = mean(r['baseline_cpd'] for r in results)
    portfolio_cpd_mean = mean(r['portfolio_cpd'] for r in results)
    baseline_cpd_std = stdev(r['baseline_cpd'] for r in results) if num_seeds > 1 else 0
    portfolio_cpd_std = stdev(r['portfolio_cpd'] for r in results) if num_seeds > 1 else 0

    wl_changes = [((r['baseline_wirelength'] - r['portfolio_wirelength']) / r['baseline_wirelength'] * 100)
                  for r in results]
    wl_ratios = [r['portfolio_wirelength'] / r['baseline_wirelength'] for r in results]
    wl_change_geomean = (geometric_mean(wl_ratios) - 1) * 100
    baseline_wl_mean = int(mean(r['baseline_wirelength'] for r in results))
    portfolio_wl_mean = int(mean(r['portfolio_wirelength'] for r in results))

    iter_changes = [((r['baseline_iterations'] - r['portfolio_iterations']) / r['baseline_iterations'] * 100)
                    for r in results]
    iter_ratios = [r['portfolio_iterations'] / r['baseline_iterations'] for r in results]
    iter_change_geomean = (geometric_mean(iter_ratios) - 1) * 100
    baseline_iter_mean = int(mean(r['baseline_iterations'] for r in results))
    portfolio_iter_mean = int(mean(r['portfolio_iterations'] for r in results))

    proxy_wallclock_mean = mean(r['proxy_wallclock'] for r in results)
    portfolio_runtime_mean = mean(r['portfolio_wallclock'] - r['proxy_wallclock'] for r in results)
    baseline_runtime_mean = mean(r['baseline_wallclock'] for r in results)
    wall_clk_pct_mean = mean(r['wall_clk_pct'] for r in results)

    print("\n" + "="*70, flush=True)
    print(f"  Summary for {num_cores} Cores - {benchmark} ({num_seeds} seeds)", flush=True)
    print("="*70, flush=True)

    print("\nPROXY SELECTION QUALITY:", flush=True)
    print(f"  Containment: {containment_total}/{num_seeds} seeds hit oracle best ({containment_pct:.0f}%)", flush=True)
    print(f"  Mean Regret: {mean_regret_avg:.3f} ns (portfolio CPD - oracle best CPD)", flush=True)

    print("\nCPD PERFORMANCE:", flush=True)
    print(f"  CPD Gain (geomean): {cpd_gain_geomean:+.2f}% [portfolio vs baseline]", flush=True)
    print(f"  CPD Gain (mean): {cpd_gain_mean:+.2f}%", flush=True)
    print(f"  Wins: {wins}/{num_seeds} seeds, Regressions: {regressions}/{num_seeds} seeds", flush=True)
    print(f"  CPD (mean): baseline {baseline_cpd_mean:.3f} ns → portfolio {portfolio_cpd_mean:.3f} ns", flush=True)
    print(f"  CPD (std): baseline {baseline_cpd_std:.3f} ns → portfolio {portfolio_cpd_std:.3f} ns", flush=True)

    print("\nWALLCLOCK OVERHEAD:", flush=True)
    print(f"  Proxy collection: {proxy_wallclock_mean:.1f}s (constant in COW fork mode)", flush=True)
    print(f"  Portfolio runtime: {portfolio_runtime_mean:.1f}s (avg selected config P+R time)", flush=True)
    print(f"  Baseline runtime: {baseline_runtime_mean:.1f}s (avg C0 P+R time)", flush=True)
    print(f"  Total overhead: {wall_clk_pct_mean:+.2f}%", flush=True)

    print("\nWIRELENGTH:", flush=True)
    print(f"  Change (geomean): {wl_change_geomean:+.2f}%", flush=True)
    print(f"  Mean: baseline {baseline_wl_mean} → portfolio {portfolio_wl_mean}", flush=True)

    print("\nITERATIONS:", flush=True)
    print(f"  Change (geomean): {iter_change_geomean:+.2f}%", flush=True)
    print(f"  Mean: baseline {baseline_iter_mean} → portfolio {portfolio_iter_mean}", flush=True)

    print("\nPER-SEED BREAKDOWN:", flush=True)
    print(f"  {'Seed':<6} | {'Baseline CPD':<13} | {'Portfolio CPD':<14} | {'Selected':<8} | {'Oracle':<8} | {'Regret':<8} | {'Gain':<7}", flush=True)
    print(f"  {'-'*6}-+-{'-'*13}-+-{'-'*14}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}-+-{'-'*7}", flush=True)

    for i, r in enumerate(results, 1):
        print(f"  {i:<6} | {r['baseline_cpd']:>10.3f} ns | {r['portfolio_cpd']:>11.3f} ns | "
              f"{r['portfolio_config']:<8} | {r['oracle_best_config']:<8} | "
              f"{r['mean_regret']:>6.3f}ns | {r['cpd_gain_pct']:>+6.2f}%")

    print("="*70 + "\n", flush=True)

def print_regression_zscore_analysis(seed_results, num_cores):
    """
    For seeds with regressions, compute the z-score of their baseline CPD
    relative to the distribution of all baseline CPDs. A strongly negative
    z-score means the baseline was already unusually good — explaining why
    the proxy-selected config couldn't beat it.
    """
    baselines = [r['baseline_cpd'] for r in seed_results]
    if len(baselines) < 2:
        return

    mu  = mean(baselines)
    sig = stdev(baselines)
    if sig == 0:
        return

    # Attach z-scores to every seed result
    for r in seed_results:
        r['baseline_zscore'] = (r['baseline_cpd'] - mu) / sig

    regression_seeds = [r for r in seed_results if r['cpd_gain_pct'] < 0]
    normal_seeds     = [r for r in seed_results if r['cpd_gain_pct'] >= 0]

    if not regression_seeds:
        return

    dist = NormalDist(mu=0, sigma=1)

    print(f"\n  REGRESSION BASELINE ANALYSIS ({num_cores} cores)", flush=True)
    print(f"  Baseline CPD distribution: μ={mu:.3f}ns  σ={sig:.3f}ns  (n={len(baselines)})", flush=True)
    print(f"  %ile = percentile of seed's baseline among all seeds. Low %ile = unusually fast baseline = hard to beat.\n", flush=True)

    # Per-regression-seed breakdown
    print(f"  {'Seed':<6} | {'Baseline CPD':<13} | {'%ile':<7} | {'Beats X% of seeds':<19} | {'CPD Gain':<9} | Ceiling strength", flush=True)
    print(f"  {'-'*6}-+-{'-'*13}-+-{'-'*7}-+-{'-'*19}-+-{'-'*9}-+-{'-'*20}", flush=True)

    for r in sorted(regression_seeds, key=lambda x: x['baseline_zscore']):
        z    = r['baseline_zscore']
        pct  = dist.cdf(z) * 100          # percentile of this baseline in the distribution
        beats = 100 - pct                  # % of other seeds whose baselines were slower
        verdict = ("Very strong" if z < -1.5 else
                   "Strong"      if z < -0.75 else
                   "Moderate"    if z < 0     else
                   "Weak")
        print(f"  {r['seed']:<6} | {r['baseline_cpd']:>10.3f} ns | {pct:>5.1f}% | "
              f"faster than {beats:>4.1f}% of seeds   | "
              f"{r['cpd_gain_pct']:>+7.2f}%  | {verdict}", flush=True)

    # Summary: mean percentile of regression vs normal seeds
    mean_pct_regress = mean(dist.cdf(r['baseline_zscore']) * 100 for r in regression_seeds)
    mean_pct_normal  = (mean(dist.cdf(r['baseline_zscore']) * 100 for r in normal_seeds)
                        if normal_seeds else float('nan'))

    print(f"\n  Mean baseline %ile — regression seeds: {mean_pct_regress:.1f}%   "
          f"non-regression seeds: {mean_pct_normal:.1f}%", flush=True)
    print(f"  (lower %ile = better baseline = harder to beat)", flush=True)

    if mean_pct_regress < 35:
        print(f"  → Regressions concentrated in seeds with unusually fast baselines (ceiling effect).", flush=True)
    else:
        print(f"  → Baseline quality alone does not explain the regressions.", flush=True)
    print(flush=True)

def main():
    parser = argparse.ArgumentParser(description='Complete proxy deployment analysis')
    parser.add_argument('--benchmark', required=True, help='Benchmark name')
    parser.add_argument('--seeds', required=True,
                        help='Seed range: either N (seeds 1..N) or X-Y (seeds X..Y inclusive)')
    parser.add_argument('--cores', required=True, help='Core counts (e.g., 1,2,4,8,12,16)')
    parser.add_argument('--clear', action='store_true', help='Delete existing proxy data and recollect')
    parser.add_argument('--max_workers', type=int, default=0,
                        help='Max parallel VPR processes (0=all 48 at once). Limits memory usage. '
                             'Proxy latency scales as ceil(48/max_workers).')
    parser.add_argument('--method', default='main',
                        help='Comma-separated methods: main (full V0+PQT), v0 (V0-only ablation), pqt (PQT-only ablation). '
                             'E.g. --method main  --method v0,pqt  --method main,v0,pqt')
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress per-core MAIN section and cross-core table; only print ablation comparison table')
    parser.add_argument('--pin_cores', action='store_true',
                        help='Pin each VPR process to a free CPU core via taskset. '
                             'Automatically selects the max_workers least-loaded cores at startup.')
    parser.add_argument('--timeout', type=int, default=3600*3,
                        help='Per-VPR-config timeout in seconds (default: 10800). '
                             'Seeds where all configs exceed this limit are skipped for proxy collection.')
    parser.add_argument('--results_dir', default='PAPER_RESULTS_KOIOS',
                        help='Root directory for all eval and proxy data (default: PAPER_RESULTS_KOIOS)')
    parser.add_argument('--proxy', type=int, default=1,
                        help='0 to skip proxy collection and only show degenerate-case results (cores >= configs)')
    parser.add_argument('--baseline', type=int, default=1,
                        help='0 to skip B.Competes/B.Forced rows in ablation table')

    args = parser.parse_args()

    global RESULTS_DIR
    RESULTS_DIR = args.results_dir

    # Resolve free cores once at startup
    if args.pin_cores:
        n_workers = args.max_workers if args.max_workers > 0 else 48
        free_cores = pick_free_cores(n_workers)
        print(f"  [CPU Pinning] Selected {len(free_cores)} free cores: {free_cores}", flush=True)
        # Store as a sorted list for use below
        args._free_cores = free_cores
    else:
        args._free_cores = None

    methods      = set(args.method.split(','))
    run_main     = 'main' in methods
    run_v0       = 'v0'   in methods
    run_pqt      = 'pqt'  in methods

    # Parse seed range: "N" → seeds 1..N, "X-Y" → seeds X..Y inclusive
    if '-' in args.seeds:
        parts = args.seeds.split('-')
        seed_start, seed_end = int(parts[0]), int(parts[1])
    else:
        seed_start, seed_end = 1, int(args.seeds)
    seed_range = (seed_start, seed_end + 1)  # used with range(*seed_range)
    num_seeds_total = seed_end - seed_start + 1

    benchmarks = [b.strip() for b in args.benchmark.split(',')]
    for benchmark in benchmarks:
        bench_dir = Path(RESULTS_DIR) / benchmark
        bench_dir.mkdir(parents=True, exist_ok=True)
        log_path  = bench_dir / "output.txt"
        with open(log_path, 'w') as _log:
            class _Tee:
                def write(self, msg):
                    sys.__stdout__.write(msg)
                    _log.write(msg)
                def flush(self):
                    sys.__stdout__.flush()
                    _log.flush()
            _old_stdout, sys.stdout = sys.stdout, _Tee()
            try:
                run_one_benchmark(benchmark, args, seed_range, num_seeds_total, seed_start, seed_end,
                                  run_main, run_v0, run_pqt)
            finally:
                sys.stdout = _old_stdout


def run_one_benchmark(benchmark, args, seed_range, num_seeds_total, seed_start, seed_end,
                      run_main, run_v0, run_pqt):
    print(f"\n{'='*70}", flush=True)
    print(f"Executing Proxy Deployment Analysis for: {benchmark}", flush=True)
    print(f"BLIF Path: koios_parmys_blif/{benchmark}/{benchmark}.parmys.blif", flush=True)
    print(f"Seeds: {seed_start}–{seed_end} ({num_seeds_total} total)", flush=True)
    print(f"{'='*70}\n", flush=True)

    active_ablations = []
    if run_main: active_ablations.append('full')
    if run_v0:   active_ablations.append('v0_only')
    if run_pqt:  active_ablations.append('pqt_only')

    _cfg_parts = [f"{ABLATION_CORES[a]+1} ({ABLATION_LABEL[a]})" for a in active_ablations]
    print(f"\n{'='*70}", flush=True)
    print(f"RUNNING/CHECKING EVALUATIONS: {', '.join(_cfg_parts)} configs", flush=True)
    print(f"{'='*70}\n", flush=True)

    eval_ablations = active_ablations[:]

    # Track available seeds per ablation
    available_seeds = {abl: [] for abl in ABLATION_LABEL}

    def _eval_one_ablation(seed, ablation):
        """Check or run one (seed, ablation) evaluation. Returns (ablation, ok, elapsed, status)."""
        eval_dir = eval_dir_path(benchmark, seed, ablation)
        eval_csv = eval_dir / "stage2_full_runs.csv"
        if args.clear and eval_dir.exists():
            shutil.rmtree(eval_dir)
        if eval_csv.exists():
            full = load_full_run_data(benchmark, seed, ablation)
            expected = ABLATION_CORES[ablation] + 1  # +1 for C0
            n_ok = len(full) if full else 0
            if n_ok == 0:
                # All configs failed — delete stale output and re-run from scratch
                shutil.rmtree(eval_dir)
                start_time = time.time()
                ok = run_full_evaluation(benchmark, seed, ablation=ablation,
                                         timeout=args.timeout,
                                         max_workers=args.max_workers,
                                         pinned_cores=args._free_cores)
                elapsed = time.time() - start_time
                new_full = load_full_run_data(benchmark, seed, ablation)
                if not new_full:
                    return ablation, False, elapsed, 'all_failed'
                return ablation, True, elapsed, 'partial'
            if n_ok >= expected:
                return ablation, True, 0.0, 'exists'
            # Partial results — re-run to attempt failed configs
            start_time = time.time()
            ok = run_full_evaluation(benchmark, seed, ablation=ablation,
                                     timeout=args.timeout,
                                     max_workers=args.max_workers,
                                     pinned_cores=args._free_cores)
            elapsed = time.time() - start_time
            new_full = load_full_run_data(benchmark, seed, ablation)
            return ablation, bool(new_full), elapsed, 'partial'
        start_time = time.time()
        ok = run_full_evaluation(benchmark, seed, ablation=ablation,
                                 timeout=args.timeout,
                                 max_workers=args.max_workers,
                                 pinned_cores=args._free_cores)
        elapsed = time.time() - start_time
        full = load_full_run_data(benchmark, seed, ablation)
        if ok and not full:
            # Eval script returned OK but no valid configs — likely all configs failed (bad seed),
            # NOT a timeout. Don't abort remaining seeds.
            return ablation, False, elapsed, 'all_failed'
        return ablation, ok, elapsed, 'run'

    aborted_ablations = set()  # ablations where a timeout was seen — no further seeds run

    for idx, seed in enumerate(range(*seed_range), 1):
        print(f"[Seed {seed:2d}]", flush=True)

        # Skip ablations already aborted by a prior seed's timeout
        seeds_to_run = [abl for abl in eval_ablations if abl not in aborted_ablations]
        for abl in eval_ablations:
            if abl in aborted_ablations:
                print(f"  [{abl}] ✗ Skipped (previous seed timed out)", flush=True)

        # Run remaining ablations for this seed in parallel
        results = {}
        if seeds_to_run:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(seeds_to_run)) as ex:
                futs = {ex.submit(_eval_one_ablation, seed, abl): abl for abl in seeds_to_run}
                for fut in concurrent.futures.as_completed(futs):
                    abl, ok, elapsed, status = fut.result()
                    results[abl] = (ok, elapsed, status)

        # Print results in stable order and detect new timeouts
        for ablation in seeds_to_run:
            ok, elapsed, status = results[ablation]
            if status == 'timeout':
                print(f"  [{ablation}] ✗ Timed out — aborting remaining seeds for this ablation", flush=True)
                aborted_ablations.add(ablation)
            elif status == 'all_failed':
                print(f"  [{ablation}] ✗ All configs failed (bad seed) — skipping | Time: {elapsed:.1f}s", flush=True)
            elif status == 'exists':
                if args.clear:
                    print(f"  [{ablation}] ✗ Cleared → re-ran | Time: {elapsed:.1f}s", flush=True)
                else:
                    print(f"  [{ablation}] ✓ exists", flush=True)
            elif status == 'partial':
                print(f"  [{ablation}] ⚠ Re-ran failed configs | Time: {elapsed:.1f}s", flush=True)
            elif ok:
                print(f"  [{ablation}] ✓ Completed | Time: {elapsed:.1f}s", flush=True)
            else:
                print(f"  [{ablation}] ✗ Failed | Time: {elapsed:.1f}s", flush=True)
            if ok and status != 'all_failed':
                available_seeds[ablation].append(seed)

        print(f"  Progress: {progress_bar(idx, num_seeds_total)}", flush=True)
        print(flush=True)

    # Report aborted ablations
    for ablation in aborted_ablations:
        print(f"  ✗ [{ABLATION_LABEL[ablation]}] No results — aborted due to timeout", flush=True)

    if run_main and not available_seeds['full']:
        print(f"\n✗ No full eval data available\n", flush=True)
        return

    for abl in active_ablations:
        ready = available_seeds[abl]
        print(f"  {ABLATION_LABEL[abl]}: {len(ready)}/{num_seeds_total} seeds ready", flush=True)
        # Warn about seeds with partial config failures
        for seed in ready:
            csv_path = eval_dir_path(benchmark, seed, abl) / "stage2_full_runs.csv"
            if not csv_path.exists():
                continue
            with open(csv_path) as _f:
                rows = list(csv.DictReader(_f))
            expected = ABLATION_CORES[abl] + 1  # +1 for C0
            ok_count = sum(1 for r in rows if r.get('status') == 'OK')
            fail_count = expected - ok_count
            if fail_count > 0:
                failed_ids = [r['candidate_id'] for r in rows if r.get('status') != 'OK']
                print(f"    ⚠ Seed {seed}: {ok_count}/{expected} configs OK "
                      f"({fail_count} failed: {','.join(failed_ids)})", flush=True)

    # Step 1: Collect proxy data for all ablations in parallel.
    # t_one in proxy_timing.json measures individual VPR run time and is unaffected by parallelism.
    if args.proxy:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(active_ablations)) as ex:
            proxy_futs = [ex.submit(collect_proxy_data, benchmark, seed_range, ablation=abl,
                                    clear=args.clear, max_workers=args.max_workers,
                                    pinned_cores=args._free_cores)
                          for abl in active_ablations]
            for f in concurrent.futures.as_completed(proxy_futs):
                f.result()  # propagate any exceptions
    else:
        print(f"  [--proxy 0] Skipping proxy collection", flush=True)

    # Step 2: Analyze each core configuration
    core_counts = [int(c.strip()) for c in args.cores.split(',')]
    effective_workers = args.max_workers if args.max_workers > 0 else 48

    # Note: --max_workers controls concurrent VPR instances (memory bound).
    # It can be less than --cores; the singlewave script batches into waves.

    # Load measured proxy collection times per ablation
    proxy_timings = {abl: load_proxy_timing(benchmark, abl) for abl in active_ablations}

    def build_core_summary(seed_results, num_cores, ablation):
        """Compute summary dict for one (ablation, core_count) combination."""
        cpd_gains    = [r['cpd_gain_pct'] for r in seed_results]
        wall_clk_pcts= [r['wall_clk_pct'] for r in seed_results]
        wl_ratios    = [r['portfolio_wirelength'] / r['baseline_wirelength'] for r in seed_results]
        iter_ratios  = [r['portfolio_iterations'] / r['baseline_iterations'] for r in seed_results]
        regrets      = [r['mean_regret'] for r in seed_results]
        containment  = sum(r['containment'] for r in seed_results)

        negative_gains   = [g for g in cpd_gains if g < 0]
        num_regressions  = len(negative_gains)
        avg_regression   = mean(negative_gains) if negative_gains else 0.0
        worst_regression = min(negative_gains) if negative_gains else 0.0

        baselines  = [r['baseline_cpd'] for r in seed_results]
        bl_mu      = mean(baselines)
        bl_sig     = stdev(baselines) if len(baselines) > 1 else 1.0
        reg_seeds  = [r for r in seed_results if r['cpd_gain_pct'] < 0]
        mean_z_reg = (mean((r['baseline_cpd'] - bl_mu) / bl_sig for r in reg_seeds)
                      if reg_seeds else float('nan'))

        bl_std   = stdev(r['baseline_cpd'] for r in seed_results) if len(seed_results) > 1 else 1.0
        port_std = stdev(r['portfolio_cpd'] for r in seed_results) if len(seed_results) > 1 else 1.0
        sdf      = bl_std / port_std if port_std > 0 else float('nan')

        return {
            'ablation': ablation,
            'cores': num_cores,
            'cpd_gain_mean': mean(cpd_gains),
            'cpd_gain_geomean': geometric_mean([1 + g/100 for g in cpd_gains]) * 100 - 100,
            'wall_clk_mean': mean(wall_clk_pcts),
            'proxy_pct_mean': mean(r['proxy_pct'] for r in seed_results),
            'run_pct_mean':   mean(r['run_pct']   for r in seed_results),
            'wl_change_geomean': (geometric_mean(wl_ratios) - 1) * 100,
            'iter_change_geomean': (geometric_mean(iter_ratios) - 1) * 100,
            'regret_mean': mean(regrets),
            'containment': containment,
            'num_seeds': len(seed_results),
            'num_regressions': num_regressions,
            'avg_regression': avg_regression,
            'worst_regression': worst_regression,
            'mean_z_regression': mean_z_reg,
            'std_decrease_factor': sdf,
            'baseline_cpd_std': bl_std,
            'portfolio_cpd_std': port_std,
        }

    all_core_summaries   = []           # full ablation only (existing cross-core table)
    ablation_summaries   = {abl: [] for abl in ABLATION_LABEL}  # all ablations, per core count

    SECTION_HEADER = {
        'full':     '█  MAIN  (V0 + PQT)',
        'v0_only':  '─  V0 ONLY  (macro ordering, no timing pressure)',
        'pqt_only': '─  PQT ONLY  (timing pressure, baseline V0)',
    }

    # Print MAIN section header once before the core-count loop
    if run_main and available_seeds.get('full') and not args.quiet:
        print(f"\n{'█'*70}", flush=True)
        print(f"█  MAIN  —  V0 + PQT  (full portfolio, {ABLATION_CORES['full']} candidates)", flush=True)
        print(f"{'█'*70}", flush=True)

    for num_cores in core_counts:
        for ablation in active_ablations:
            timing_map    = proxy_timings[ablation]
            seeds_for_abl = available_seeds[ablation]
            if not seeds_for_abl:
                continue

            # proxy_time = (n_configs / max_workers) * t_one_proxy
            def compute_proxy_time(t_one):
                # Proxy runs in parallel across min(num_cores, n_configs) workers
                parallel = min(num_cores, ABLATION_CORES[ablation])
                return (ABLATION_CORES[ablation] / parallel) * t_one

            mean_t_one = mean(timing_map[s] for s in seeds_for_abl if s in timing_map) \
                         if any(s in timing_map for s in seeds_for_abl) else 0.0
            mean_proxy_time = compute_proxy_time(mean_t_one)

            is_main = (ablation == 'full')
            if is_main and run_main and not args.quiet:
                print(f"\n{'─'*70}", flush=True)
                print(f"  {num_cores} cores | proxy {mean_proxy_time:.1f}s | {len(seeds_for_abl)} seeds", flush=True)
                print(f"{'─'*70}", flush=True)

            seed_results  = []
            running_gains = []
            running_wc    = []

            for idx, seed in enumerate(seeds_for_abl, 1):
                try:
                    proxy_data    = load_proxy_data(benchmark, seed, ablation)
                    full_run_data = load_full_run_data(benchmark, seed, ablation)
                    # Use full eval_48c C0 as canonical baseline when available, else use own C0
                    if ablation == 'full' or not available_seeds.get('full'):
                        full_run_data_canonical = full_run_data
                    else:
                        full_run_data_canonical = load_full_run_data(benchmark, seed, 'full')
                    canonical_baseline = full_run_data_canonical.get('C0')
                    seed_proxy_time = compute_proxy_time(timing_map.get(seed, 0.0))
                    strat  = 'no_c0' if ablation in ('v0_only', 'pqt_only') else 'normal'
                    result = analyze_seed(full_run_data, proxy_data,
                                          min(num_cores, ABLATION_CORES[ablation]),
                                          seed_proxy_time,
                                          canonical_baseline=canonical_baseline,
                                          strategy=strat)
                    result['seed'] = seed
                    seed_results.append(result)
                    running_gains.append(result['cpd_gain_pct'])
                    running_wc.append(result['wall_clk_pct'])

                    if is_main and run_main and not args.quiet:
                        print(f"    Seed {seed:2d}  gain {result['cpd_gain_pct']:>+7.2f}%  "
                              f"wall_clk {result['wall_clk_pct']:>6.1f}%  "
                              f"running {mean(running_gains):>+7.2f}%", flush=True)

                except (FileNotFoundError, ValueError) as e:
                    print(f"  Warning [{ablation}] seed {seed}: {e}\n", flush=True)
                    continue

            if is_main and run_main and seed_results and not args.quiet:
                print(f"{'─'*70}", flush=True)

            if seed_results:
                s = build_core_summary(seed_results, num_cores, ablation)
                ablation_summaries[ablation].append(s)
                if ablation == 'full':
                    all_core_summaries.append(s)

    # ── V0 (Baseline Competes) and V1 (Baseline Forced) strategy rows ──────────
    if run_v0 or run_pqt:
        # Collect C0 proxy metrics once (cached) for B.Competes strategies
        c0_proxy_metrics = {}
        if available_seeds.get('v0_only') or available_seeds.get('pqt_only'):
            c0_proxy_metrics = collect_c0_proxy_data(
                benchmark, list(range(*seed_range)),
                max_workers=args.max_workers or 8)

        for num_cores in core_counts:
            def _run_strategy(base_ablation, strategy_key, strategy='normal', augment_c0=False):
                timing_map   = proxy_timings.get(base_ablation, {})
                seed_results = []
                for seed in available_seeds.get(base_ablation, []):
                    try:
                        proxy_data    = load_proxy_data(benchmark, seed, base_ablation)
                        full_run_data = load_full_run_data(benchmark, seed, base_ablation)
                        canonical     = (load_full_run_data(benchmark, seed, 'full').get('C0')
                                         if available_seeds.get('full') else None)
                        pd = ({**proxy_data, 'C0': c0_proxy_metrics[seed]}
                              if augment_c0 and seed in c0_proxy_metrics else proxy_data)
                        parallel  = min(num_cores, ABLATION_CORES[base_ablation])
                        seed_pt   = (ABLATION_CORES[base_ablation] / parallel) * timing_map.get(seed, 0.0)
                        result    = analyze_seed(full_run_data, pd,
                                                 min(num_cores, ABLATION_CORES[base_ablation]),
                                                 seed_pt, canonical_baseline=canonical,
                                                 strategy=strategy)
                        result['seed'] = seed
                        seed_results.append(result)
                    except (FileNotFoundError, ValueError):
                        continue
                if seed_results:
                    ablation_summaries[strategy_key].append(
                        build_core_summary(seed_results, num_cores, strategy_key))

            if available_seeds.get('v0_only') and c0_proxy_metrics:
                _run_strategy('v0_only', 'v0_compete',  strategy='normal', augment_c0=True)
            if available_seeds.get('v0_only'):
                _run_strategy('v0_only', 'v0_forced',   strategy='forced')
            if available_seeds.get('pqt_only') and c0_proxy_metrics:
                _run_strategy('pqt_only', 'pqt_compete', strategy='normal', augment_c0=True)
            if available_seeds.get('pqt_only'):
                _run_strategy('pqt_only', 'pqt_forced',  strategy='forced')

    # Print cross-core comparison summary
    if all_core_summaries and not args.quiet:
        print("\n" + "="*120, flush=True)
        print("  CROSS-CORE COMPARISON SUMMARY", flush=True)
        print("="*120, flush=True)
        
        # Column widths — sized to fit worst-case content exactly
        # geo/mean: "+XX.XX%" = 8 chars  regret: "XXX.XXX ns" = 10  overhead: "+XXX.X%" = 7
        # wl/it: "+XX.XX%" = 8  contain: "25/25 (100%)" = 12  regress: see below  pct: "XX.X%" = 5
        C = dict(cores=6, geo=12, mean_gain=12, std_fac=8, regret=12, wall_clk=10,
                 wl=9, it=9, contain=14, regress=36, pct=12)

        header = (f"  {'Cores':<{C['cores']}} | {'Gain(Geo)':<{C['geo']}} | "
                  f"{'Gain(Mean)':<{C['mean_gain']}} | {'Std↓':<{C['std_fac']}} | "
                  f"{'Regret':<{C['regret']}} | {'Wall Clk Δ':<{C['wall_clk']}} | "
                  f"{'WL Δ':<{C['wl']}} | {'Iter Δ':<{C['it']}} | "
                  f"{'Containment':<{C['contain']}} | {'Regressions':<{C['regress']}} | {'Regress %ile':<{C['pct']}}")
        sep    = (f"  {'-'*C['cores']}-+-{'-'*C['geo']}-+-{'-'*C['mean_gain']}-+-"
                  f"{'-'*C['std_fac']}-+-{'-'*C['regret']}-+-{'-'*C['wall_clk']}-+-"
                  f"{'-'*C['wl']}-+-{'-'*C['it']}-+-{'-'*C['contain']}-+-"
                  f"{'-'*C['regress']}-+-{'-'*C['pct']}")

        print(f"\n{header}", flush=True)
        print(sep, flush=True)

        dist = NormalDist(mu=0, sigma=1)
        for s in all_core_summaries:
            geo_str      = f"{s['cpd_gain_geomean']:>+7.2f}%"
            mean_str     = f"{s['cpd_gain_mean']:>+7.2f}%"
            sdf          = s['std_decrease_factor']
            sdf_str      = f"{sdf:.2f}x" if not math.isnan(sdf) else "  n/a"
            regret_str   = f"{s['regret_mean']:>8.3f} ns"
            wall_clk_str = f"{s['wall_clk_mean']:>7.1f}%"
            wl_str       = f"{s['wl_change_geomean']:>+7.2f}%"
            it_str       = f"{s['iter_change_geomean']:>+7.2f}%"
            contain_str  = f"{s['containment']}/{s['num_seeds']} ({s['containment']/s['num_seeds']*100:>3.0f}%)"
            if s['num_regressions'] > 0:
                regress_str = (f"{s['num_regressions']}/{s['num_seeds']}"
                               f"  avg:{s['avg_regression']:+.1f}%  worst:{s['worst_regression']:+.1f}%")
                z = s['mean_z_regression']
                pct_str = f"{dist.cdf(z)*100:.1f}%" if not math.isnan(z) else "  n/a"
            else:
                regress_str = f"0/{s['num_seeds']}  none"
                pct_str = "    n/a"

            print(f"  {s['cores']:<{C['cores']}} | {geo_str:<{C['geo']}} | "
                  f"{mean_str:<{C['mean_gain']}} | {sdf_str:<{C['std_fac']}} | "
                  f"{regret_str:<{C['regret']}} | {wall_clk_str:<{C['wall_clk']}} | "
                  f"{wl_str:<{C['wl']}} | {it_str:<{C['it']}} | "
                  f"{contain_str:<{C['contain']}} | {regress_str:<{C['regress']}} | {pct_str:<{C['pct']}}", flush=True)

        print("="*120, flush=True)

    # Print ablation comparison table
    has_ablation = any(ablation_summaries[abl] for abl in ['v0_only', 'pqt_only', 'v0_compete', 'v0_forced', 'pqt_compete', 'pqt_forced'])
    if has_ablation:
        print("\n" + "="*120, flush=True)
        print("  ABLATION COMPARISON  (V0 Only = macro ordering only | PQT Only = timing pressure, baseline V0 | V0+PQT = full)", flush=True)
        print("="*120, flush=True)

        C = dict(abl=16, cores=6, geo=12, mean_gain=12, std_fac=8, regret=12, wall_clk=10,
                 proxy_clk=10, run_clk=10,
                 wl=9, it=9, contain=14, regress=36, pct=12)

        header = (f"  {'Condition':<{C['abl']}} | {'Cores':<{C['cores']}} | {'Gain(Geo)':<{C['geo']}} | "
                  f"{'Gain(Mean)':<{C['mean_gain']}} | {'Std↓':<{C['std_fac']}} | "
                  f"{'Regret':<{C['regret']}} | {'Wall Clk Δ':<{C['wall_clk']}} | "
                  f"{'Proxy Δ':<{C['proxy_clk']}} | {'Run Δ':<{C['run_clk']}} | "
                  f"{'WL Δ':<{C['wl']}} | {'Iter Δ':<{C['it']}} | "
                  f"{'Containment':<{C['contain']}} | {'Regressions':<{C['regress']}} | {'Regress %ile':<{C['pct']}}")
        sep = (f"  {'-'*C['abl']}-+-{'-'*C['cores']}-+-{'-'*C['geo']}-+-{'-'*C['mean_gain']}-+-"
               f"{'-'*C['std_fac']}-+-{'-'*C['regret']}-+-{'-'*C['wall_clk']}-+-"
               f"{'-'*C['proxy_clk']}-+-{'-'*C['run_clk']}-+-"
               f"{'-'*C['wl']}-+-{'-'*C['it']}-+-{'-'*C['contain']}-+-"
               f"{'-'*C['regress']}-+-{'-'*C['pct']}")

        dist = NormalDist(mu=0, sigma=1)

        def print_ablation_row(s, C, dist):
            abl_str       = ABLATION_LABEL[s['ablation']]
            geo_str       = f"{s['cpd_gain_geomean']:>+7.2f}%"
            mean_str      = f"{s['cpd_gain_mean']:>+7.2f}%"
            sdf           = s['std_decrease_factor']
            sdf_str       = f"{sdf:.2f}x" if not math.isnan(sdf) else "  n/a"
            regret_str    = f"{s['regret_mean']:>8.3f} ns"
            wall_clk_str  = f"{s['wall_clk_mean']:>7.1f}%"
            proxy_clk_str = f"{s['proxy_pct_mean']:>7.1f}%"
            run_clk_str   = f"{s['run_pct_mean']:>+7.1f}%"
            wl_str        = f"{s['wl_change_geomean']:>+7.2f}%"
            it_str        = f"{s['iter_change_geomean']:>+7.2f}%"
            contain_str   = f"{s['containment']}/{s['num_seeds']} ({s['containment']/s['num_seeds']*100:>3.0f}%)"
            if s['num_regressions'] > 0:
                regress_str = (f"{s['num_regressions']}/{s['num_seeds']}"
                               f"  avg:{s['avg_regression']:+.1f}%  worst:{s['worst_regression']:+.1f}%")
                z       = s['mean_z_regression']
                pct_str = f"{dist.cdf(z)*100:.1f}%" if not math.isnan(z) else "  n/a"
            else:
                regress_str = f"0/{s['num_seeds']}  none"
                pct_str = "    n/a"
            print(f"    {abl_str:<{C['abl']}} | {s['cores']:<{C['cores']}} | {geo_str:<{C['geo']}} | "
                  f"{mean_str:<{C['mean_gain']}} | {sdf_str:<{C['std_fac']}} | "
                  f"{regret_str:<{C['regret']}} | {wall_clk_str:<{C['wall_clk']}} | "
                  f"{proxy_clk_str:<{C['proxy_clk']}} | {run_clk_str:<{C['run_clk']}} | "
                  f"{wl_str:<{C['wl']}} | {it_str:<{C['it']}} | "
                  f"{contain_str:<{C['contain']}} | {regress_str:<{C['regress']}} | {pct_str:<{C['pct']}}", flush=True)

        print(f"\n{header}", flush=True)
        print(sep, flush=True)
        for i, nc in enumerate(core_counts):
            if i > 0:
                print(sep, flush=True)

            skip_compete = not args.proxy  # B.Competes is redundant without proxy
            for ablation in ['v0_only', 'v0_compete', 'v0_forced', 'pqt_only', 'pqt_compete', 'pqt_forced']:
                if skip_compete and ablation in ('v0_compete', 'pqt_compete'):
                    continue
                matches = [s for s in ablation_summaries[ablation] if s['cores'] == nc]
                if matches:
                    print_ablation_row(matches[0], C, dist)

        print("="*120, flush=True)

    # ── 8-Config PQT: λ Pair Search Space Reduction Table ───────────────────
    if run_pqt and available_seeds.get('pqt_only'):
        import itertools
        LAMBDA_VALS  = ['0.05', '0.10', '0.20', '0.35']
        lambda_pairs = list(itertools.combinations(LAMBDA_VALS, 2))
        show_cores   = [nc for nc in core_counts if nc <= 8] if args.proxy else [nc for nc in core_counts if nc == 8]
        timing_map_pqt = proxy_timings.get('pqt_only', {})
        pqt_seeds      = available_seeds['pqt_only']

        # Inject a temporary key into ABLATION_LABEL so print_ablation_row works
        ABLATION_LABEL['_lp'] = ''   # placeholder — overwritten per row

        print("\n" + "="*120, flush=True)
        print("  8-CONFIG PQT λ PAIR ANALYSIS  (half search space — 2 λ values × 2α × 2β = 8 configs)", flush=True)
        print("="*120, flush=True)

        dist2 = NormalDist(mu=0, sigma=1)

        C = dict(abl=16, cores=6, geo=12, mean_gain=12, std_fac=8, regret=12, wall_clk=10,
                 proxy_clk=10, run_clk=10,
                 wl=9, it=9, contain=14, regress=36, pct=12)

        header = (f"  {'Condition':<{C['abl']}} | {'Cores':<{C['cores']}} | {'Gain(Geo)':<{C['geo']}} | "
                  f"{'Gain(Mean)':<{C['mean_gain']}} | {'Std↓':<{C['std_fac']}} | "
                  f"{'Regret':<{C['regret']}} | {'Wall Clk Δ':<{C['wall_clk']}} | "
                  f"{'Proxy Δ':<{C['proxy_clk']}} | {'Run Δ':<{C['run_clk']}} | "
                  f"{'WL Δ':<{C['wl']}} | {'Iter Δ':<{C['it']}} | "
                  f"{'Containment':<{C['contain']}} | {'Regressions':<{C['regress']}} | {'Regress %ile':<{C['pct']}}")
        sep = (f"  {'-'*C['abl']}-+-{'-'*C['cores']}-+-{'-'*C['geo']}-+-{'-'*C['mean_gain']}-+-"
               f"{'-'*C['std_fac']}-+-{'-'*C['regret']}-+-{'-'*C['wall_clk']}-+-"
               f"{'-'*C['proxy_clk']}-+-{'-'*C['run_clk']}-+-"
               f"{'-'*C['wl']}-+-{'-'*C['it']}-+-{'-'*C['contain']}-+-"
               f"{'-'*C['regress']}-+-{'-'*C['pct']}")

        def print_lp_row(s, label, C, dist):
            """Like print_ablation_row but uses an explicit label string."""
            ABLATION_LABEL['_lp'] = label
            s2 = dict(s); s2['ablation'] = '_lp'
            print_ablation_row(s2, C, dist)

        print(f"\n{header}", flush=True)
        print(sep, flush=True)

        for ci, nc in enumerate(show_cores):
            if ci > 0:
                print(sep, flush=True)

            for pi, (lam_a, lam_b) in enumerate(lambda_pairs):
                lambda_set = {lam_a, lam_b}

                lp_strategies = [(f'{lam_a}+{lam_b}',  'no_c0',  False)]
                if args.baseline:
                    if args.proxy:
                        lp_strategies.append(('  B.Competes', 'normal', True))
                    lp_strategies.append(('  B.Forced', 'forced', False))
                for strategy_name, strategy, augment_c0 in lp_strategies:
                    seed_results = []
                    for seed in pqt_seeds:
                        try:
                            proxy_data_full = load_proxy_data(benchmark, seed, 'pqt_only')
                            full_run_data   = load_full_run_data(benchmark, seed, 'pqt_only')
                            cand_meta       = load_candidate_meta(benchmark, seed, 'pqt_only')

                            lam_floats  = {float(l) for l in lambda_set}
                            lambda_cids = {cid for cid, m in cand_meta.items()
                                           if m.get('lambda') and
                                           float(m['lambda']) in lam_floats}
                            pd_8 = {cid: v for cid, v in proxy_data_full.items()
                                    if cid in lambda_cids}
                            if not pd_8:
                                continue
                            if augment_c0 and seed in c0_proxy_metrics:
                                pd_8 = {**pd_8, 'C0': c0_proxy_metrics[seed]}

                            canonical_baseline = None
                            if available_seeds.get('full'):
                                try:
                                    canonical_baseline = load_full_run_data(
                                        benchmark, seed, 'full').get('C0')
                                except Exception:
                                    pass

                            t_one = timing_map_pqt.get(seed)
                            if t_one is None:
                                continue
                            proxy_time_8 = (8 / min(nc, 8)) * t_one

                            r = analyze_seed(full_run_data, pd_8, min(nc, 8),
                                             proxy_time_8,
                                             canonical_baseline=canonical_baseline,
                                             strategy=strategy)
                            seed_results.append(r)
                        except (KeyError, ValueError, FileNotFoundError):
                            pass

                    if not seed_results:
                        continue

                    s = build_core_summary(seed_results, nc, '_lp')
                    print_lp_row(s, strategy_name, C, dist2)

                if pi < len(lambda_pairs) - 1:
                    print(sep, flush=True)

        del ABLATION_LABEL['_lp']
        print("="*120, flush=True)

    # ── Per-λ Breakdown Table ──────────────────────────────────────────
    if run_pqt:
        try:
            from print_lambda_breakdown import print_lambda_breakdown
            print_lambda_breakdown(
                RESULTS_DIR, [benchmark], blif_dir="koios_parmys_blif",
                eval_dir_template="{results_dir}/{bench}/eval_pqtonly_seed{seed}"
            )
        except Exception as e:
            print(f"  Warning: could not generate λ breakdown: {e}", flush=True)


if __name__ == '__main__':
    main()
