#!/usr/bin/env python3
import argparse
import os
import json
import csv
import subprocess
import time
import re
import concurrent.futures
from collections import defaultdict

PQT_TEMPLATES = {
    "P0": {"lambda": 0.20, "alpha": 0.03, "beta": 3.0, "qstart": 0.15, "qend": 0.05},
    "P1": {"lambda": 0.10, "alpha": 0.01, "beta": 2.0, "qstart": 0.15, "qend": 0.05},
    "P2": {"lambda": 0.35, "alpha": 0.03, "beta": 4.0, "qstart": 0.15, "qend": 0.05},
    "P3": {"lambda": 0.05, "alpha": 0.03, "beta": 4.0, "qstart": 0.15, "qend": 0.05},
}

FAM_REPS = {"F0": 0, "F1": 5, "F2": 6, "F3": 9, "F4": 20, "F5": 23}
BASE_FAM_ORDER = ["F0", "F1", "F2", "F4", "F3", "F5"]

def extract_features(blif_path):
    fanouts = defaultdict(int)
    adj = defaultdict(list)
    inputs = set()
    outputs = set()
    nodes = set()
    
    with open(blif_path, 'r') as f:
        for line in f:
            line = line.split('#')[0].strip()
            if not line: continue
            if line.startswith('.inputs '):
                for x in line.split()[1:]: inputs.add(x)
            elif line.startswith('.outputs '):
                for x in line.split()[1:]: outputs.add(x)
            elif line.startswith('.names '):
                tokens = line.split()
                out_node = tokens[-1]
                in_nodes = tokens[1:-1]
                nodes.add(out_node)
                for in_n in in_nodes:
                    adj[in_n].append(out_node)
                    fanouts[in_n] += 1
            elif line.startswith(('.latch ', '.latch\t')):
                tokens = line.split()
                if len(tokens) >= 3:
                    in_n = tokens[1]
                    out_n = tokens[2]
                    adj[in_n].append(out_n)
                    fanouts[in_n] += 1
                    nodes.add(out_n)

    num_blocks = len(nodes)
    num_nets = len(fanouts)
    max_fo = max(fanouts.values()) if fanouts else 0
    avg_fo = sum(fanouts.values()) / max(1, num_nets)
    io_ratio = (len(inputs) + len(outputs)) / max(1, num_blocks)
    
    in_degree = defaultdict(int)
    for u in adj:
        for v in adj[u]:
            in_degree[v] += 1
            
    q = [u for u in inputs | nodes if in_degree[u] == 0]
    depth = defaultdict(int)
    max_depth = 0
    while q:
        u = q.pop(0)
        d = depth[u]
        max_depth = max(max_depth, d)
        for v in adj[u]:
            depth[v] = max(depth[v], d + 1)
            in_degree[v] -= 1
            if in_degree[v] == 0:
                q.append(v)
                
    features = {
        "num_blocks": num_blocks,
        "num_nets": num_nets,
        "avg_fanout": avg_fo,
        "max_fanout": max_fo,
        "max_logic_depth": max_depth,
        "mean_logic_depth": sum(depth.values()) / max(1, len(depth)) if depth else 0,
        "reconvergence": 0.5,
        "io_anchoring_ratio": io_ratio
    }
    return features

def parse_vpr_log(log_path):
    cpd = fmax = v0hash = iterations = None
    status = "FAIL"
    if not os.path.exists(log_path):
        return cpd, fmax, v0hash, iterations, status

    with open(log_path, 'r') as f:
        text = f.read()

    for m in re.finditer(r"Final critical path delay.*?:\s*([\d.]+)\s*ns", text):
        cpd = float(m.group(1))
    for m in re.finditer(r"Fmax:\s*([\d.]+)\s*MHz", text):
        fmax = float(m.group(1))
    m = re.search(r"\[V0_HASH\][^\n]*hash=(\d+)", text)
    if m: v0hash = m.group(1)
    
    m_iter = re.search(r"Routing iteration:\s*(\d+)", text)
    if m_iter: iterations = int(m_iter.group(1))

    if cpd is not None and not re.search(r"[Rr]outing failed|[Uu]nable to route", text):
        status = "OK"

    return cpd, fmax, v0hash, iterations, status

def run_candidate(cand, seed, out_dir, micro_screen):
    # Use absolute paths for the architecture and BLIF
    base_dir = os.path.abspath(os.getcwd())
    arch_path = os.path.join(base_dir, "vtr_flow", "arch", "timing", "k6_frac_N10_mem32K_40nm.xml")
    blif_path = os.path.join(base_dir, "vtr_flow", "benchmarks", "blif", "alu4.blif")
    vpr_bin = os.path.join(base_dir, "build", "vpr", "vpr")
    
    cmd = [
        vpr_bin,
        arch_path,
        blif_path,
        "--route_chan_width", "100",
        "--seed", str(seed),
        "--disp", "off"
    ]
    
    if micro_screen == "on":
        cmd += ["--place_quench_only", "on", "--prob_timing_enable", "off", "--prob_timing_inject", "off"]
        if cand["type"] == "joint_pair":
            cmd += ["--v0_enable", "on", "--v0_ordering_mode", "stable", "--prob_config_id", str(cand["v0_cfg"])]
        else:
            cmd += ["--v0_enable", "off"]
    else:
        if cand["type"] == "baseline":
            cmd += ["--v0_enable", "off", "--prob_timing_enable", "off", "--prob_timing_inject", "off"]
        elif cand["type"] == "pqt_only":
            cmd += ["--v0_enable", "off", "--prob_config_id", "0"]
            cmd += [
                "--prob_timing_inject", "on", "--prob_timing_enable", "on", "--prob_timing_mode", "4",
                "--prob_inject_mode", "quantile",
                "--prob_inject_lambda", "0.20",
                "--prob_timing_alpha", "0.03",
                "--prob_timing_beta", "3.0",
                "--prob_inject_quantile_start", "0.15",
                "--prob_inject_quantile_end", "0.05"
            ]
        elif cand["type"] == "joint_pair":
            cmd += ["--v0_enable", "on", "--v0_ordering_mode", "stable", "--prob_config_id", str(cand["v0_cfg"])]
            p_tm = PQT_TEMPLATES[cand["pqt_template"]]
            cmd += [
                "--prob_timing_inject", "on", "--prob_timing_enable", "on", "--prob_timing_mode", "4",
                "--prob_inject_mode", "quantile",
                "--prob_inject_lambda", str(p_tm["lambda"]),
                "--prob_timing_alpha", str(p_tm["alpha"]),
                "--prob_timing_beta", str(p_tm["beta"]),
                "--prob_inject_quantile_start", str(p_tm["qstart"]),
                "--prob_inject_quantile_end", str(p_tm["qend"])
            ]

    suffix = "_micro" if micro_screen == "on" else ""
    log_name = f"{cand['id']}_{cand['type']}{suffix}.log"
    log_path = os.path.abspath(os.path.join(out_dir, log_name))
    
    # Create an isolated run directory for this job to prevent VPR output file collisions
    job_dir = os.path.join(os.path.abspath(out_dir), f"job_{cand['id']}{suffix}")
    os.makedirs(job_dir, exist_ok=True)
    
    t0 = time.time()
    try:
        with open(log_path, 'w') as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True, timeout=900, cwd=job_dir)
    except subprocess.TimeoutExpired:
        if micro_screen == "off":
            with open(log_path, 'a') as f: f.write("\nTIMEOUT\n")
    
    t_run = time.time() - t0
    
    if micro_screen == "on":
        return cand["id"], None, None, None, "OK", t_run, log_path
        
    cpd, fmax, v0hash, iters, status = parse_vpr_log(log_path)
    return cand["id"], cpd, fmax, v0hash, status, t_run, log_path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--cores", type=int, choices=[8, 12, 16], required=True)
    parser.add_argument("--waves", type=int, choices=[1, 2, 3], required=True)
    parser.add_argument("--out_dir", type=str, required=True)
    parser.add_argument("--micro_screen", choices=["on", "off"], default="off")
    parser.add_argument("--dry_run", action="store_true", help="Print candidates without running VPR")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    
    # STAGE 0 - Extract Features
    blif_path = "vtr_flow/benchmarks/blif/alu4.blif"
    features = extract_features(blif_path)
    
    with open(os.path.join(args.out_dir, "stage0_graph_features.json"), "w") as f:
        json.dump(features, f, indent=2)
        
    fams = BASE_FAM_ORDER[:]
    if features["max_logic_depth"] > 10:
        for f in ["F2", "F1"]:
            if f in fams: fams.remove(f); fams.insert(0, f)
    if features["max_fanout"] > 50 or features["io_anchoring_ratio"] > 0.1:
        if "F4" in fams: fams.remove("F4"); fams.insert(0, "F4")
    if features["reconvergence"] > 0.4:
        if "F3" in fams: fams.remove("F3"); fams.insert(0, "F3")
        
    pqt_prefs = ["P0"]
    if features["max_logic_depth"] > 10:
        pqt_prefs.extend(["P2", "P1", "P3"])
    elif features["reconvergence"] > 0.4 or features["max_fanout"] > 50:
        pqt_prefs.extend(["P1", "P3", "P2"])
    else:
        pqt_prefs.extend(["P1", "P2", "P3"])
        
    final_pqt = []
    seen = set()
    for p in pqt_prefs:
        if p not in seen:
            final_pqt.append(p)
            seen.add(p)
            
    b_pairs = args.cores * args.waves - 2
    if b_pairs <= 6:
        n_fam, n_pqt = 3, 2
        b_case = 'A'
    elif b_pairs <= 12:
        n_fam, n_pqt = 4, 3
        b_case = 'B'
    elif b_pairs <= 18:
        n_fam, n_pqt = 6, 3
        b_case = 'C'
    else:
        n_fam, n_pqt = 6, 4
        b_case = 'D'
        
    sel_fams = fams[:n_fam]
    sel_pqts = final_pqt[:n_pqt]
    
    with open(os.path.join(args.out_dir, "stage0_family_selection.json"), "w") as f:
        json.dump({
            "budget_case": b_case,
            "b_pairs": b_pairs,
            "selected_families": sel_fams,
            "selected_reps": [FAM_REPS[x] for x in sel_fams],
            "selected_pqt_templates": sel_pqts
        }, f, indent=2)

    # STAGE 1 - Portfolio
    candidates = []
    candidates.append({"id": "C0", "type": "baseline", "v0_family": "", "v0_cfg": "", "pqt_template": ""})
    candidates.append({"id": "C1", "type": "pqt_only", "v0_family": "", "v0_cfg": "", "pqt_template": "P0"})
    
    cid = 2
    for f in sel_fams:
        for p in sel_pqts:
            candidates.append({
                "id": f"C{cid}", "type": "joint_pair",
                "v0_family": f, "v0_cfg": FAM_REPS[f], "pqt_template": p
            })
            cid += 1
            
    with open(os.path.join(args.out_dir, "portfolio_candidates.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["candidate_id", "type", "v0_family", "v0_cfg", "pqt_template", "lambda", "alpha", "beta", "qstart", "qend"])
        for c in candidates:
            p_tm = PQT_TEMPLATES.get(c["pqt_template"], {})
            w.writerow([
                c["id"], c["type"], c["v0_family"], c["v0_cfg"], c["pqt_template"],
                p_tm.get("lambda", ""), p_tm.get("alpha", ""), p_tm.get("beta", ""),
                p_tm.get("qstart", ""), p_tm.get("qend", "")
            ])

    if args.dry_run:
        print(f"DRY RUN. Case {b_case}. Pair budget: {b_pairs}. Total candidates: {len(candidates)}")
        return

    # STAGE 2 - Run Full P+R
    runs = []
    
    if args.micro_screen == "on":
        print(f"Running micro screen over {len(candidates)} candidates.")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.cores) as executor:
            futs = {executor.submit(run_candidate, c, args.seed, args.out_dir, "on"): c for c in candidates}
            for future in concurrent.futures.as_completed(futs):
                pass 
                
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.cores) as executor:
        futs = {executor.submit(run_candidate, c, args.seed, args.out_dir, "off"): c for c in candidates}
        for future in concurrent.futures.as_completed(futs):
            try:
                c_id, cpd, fmax, v0_hash, st, t_r, log_p = future.result()
                c = futs[future]
                runs.append({
                    "candidate_id": c_id,
                    "type": c["type"],
                    "seed": args.seed,
                    "v0_cfg": c["v0_cfg"],
                    "pqt_template": c["pqt_template"],
                    "cpd_ns": cpd if cpd else "",
                    "fmax_mhz": fmax if fmax else "",
                    "status": st,
                    "runtime_s": round(t_r, 2),
                    "logfile_path": log_p
                })
            except Exception as e:
                print(f"Run failed: {e}")

    runs.sort(key=lambda x: int(x["candidate_id"][1:]))

    seed_runs_csv = os.path.join(args.out_dir, "stage2_full_runs.csv")
    with open(seed_runs_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["candidate_id", "type", "seed", "v0_cfg", "pqt_template", "cpd_ns", "fmax_mhz", "status", "runtime_s", "logfile_path"])
        w.writeheader()
        w.writerows(runs)

    # STAGE 3 - Evaluate
    valid_runs = [r for r in runs if r["status"] == "OK" and r["cpd_ns"] != ""]
    if not valid_runs:
        print("All runs failed!")
        return

    best = min(valid_runs, key=lambda x: float(x["cpd_ns"]))
    base_run = next((r for r in valid_runs if r["type"] == "baseline"), None)
    
    delta = ""
    if base_run:
        delta = f"{(float(best['cpd_ns']) - float(base_run['cpd_ns'])):.4f} ns"

    with open(os.path.join(args.out_dir, "final_choice.txt"), "w") as f:
        f.write(f"Winning candidate: {best['candidate_id']}\n")
        f.write(f"Winning CPD: {best['cpd_ns']} ns\n")
        f.write(f"Delta vs baseline: {delta}\n")
        f.write(f"Winner type: {best['type']}\n")

    valid_runs.sort(key=lambda x: float(x["cpd_ns"]))
    with open(os.path.join(args.out_dir, "summary.txt"), "w") as f:
        f.write(f"Cores: {args.cores}\n")
        f.write(f"Waves: {args.waves}\n")
        f.write(f"Total Budget: {args.cores * args.waves}\n")
        f.write(f"Pair Budget: {b_pairs}\n")
        f.write(f"Selected Families: {', '.join(sel_fams)}\n")
        f.write(f"Selected PQT Templates: {', '.join(sel_pqts)}\n\n")
        f.write("Candidate CPDs (sorted):\n")
        for r in valid_runs:
            f.write(f"  {r['candidate_id']:4} ({r['type']:10}): {float(r['cpd_ns']):.4f} ns\n")
        f.write(f"\nFinal Winner: {best['candidate_id']} ({float(best['cpd_ns']):.4f} ns)\n")


if __name__ == "__main__":
    main()
