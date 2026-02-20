
import re
import sys

def parse_cpd_wns(filename):
    cpd = None
    wns = None
    runtime = None
    updates = 0
    with open(filename, 'r') as f:
        for line in f:
            if "Final critical path delay" in line:
                m = re.search(r"([\d\.]+) ns", line)
                if m: cpd = float(m.group(1))
            if "Final setup Worst Negative Slack" in line:
                m = re.search(r"([-\d\.]+) ns", line)
                if m: wns = float(m.group(1))
            if "The entire flow of VPR took" in line:
                m = re.search(r"([\d\.]+) seconds", line)
                if m: runtime = float(m.group(1))
            if "PROB_INJECT_STEP1" in line or "PROB_STEP3_REPLACE" in line:
                updates += 1
    return cpd, wns, runtime, updates

def parse_deltas(filename):
    deltas = []
    with open(filename, 'r') as f:
        for line in f:
            if "delta_cost=" in line:
                m = re.search(r"delta_cost=([-\d\.eE]+)", line)
                if m: deltas.append(float(m.group(1)))
    return deltas

def parse_schedule(filename, mode):
    events = None
    executed = None
    missed = None
    hash_val = None
    with open(filename, 'r') as f:
        content = f.read()
        if mode == "record":
            if "TIMING_SCHEDULE_BASELINE" in content:
                m = re.search(r"events=(\d+)", content)
                if m: events = int(m.group(1))
                m = re.search(r"schedule_hash=(\d+)", content)
                if m: hash_val = int(m.group(1))
        elif mode == "replay":
            if "TIMING_SCHEDULE_REPLAY_SUMMARY" in content:
                m = re.search(r"scheduled_events=(\d+)", content)
                if m: events = int(m.group(1))
                m = re.search(r"executed_events=(\d+)", content)
                if m: executed = int(m.group(1))
                m = re.search(r"missed_events=(\d+)", content)
                if m: missed = int(m.group(1))
                m = re.search(r"replay_hash=(\d+)", content)
                if m: hash_val = int(m.group(1))
    return events, executed, missed, hash_val

def main():
    report = []
    report.append("# Phase 15 Step 3 & 3.1 Validation Report")
    
    # Part B
    report.append("## Part B: Step 3 Validation")
    
    # Case A
    cpd_a, wns_a, rt_a, up_a = parse_cpd_wns("baseline_A.log")
    report.append(f"### Case A (Baseline)")
    report.append(f"- CPD: {cpd_a} ns")
    report.append(f"- WNS: {wns_a} ns")
    report.append(f"- Runtime: {rt_a} s")
    
    # Case B
    cpd_b, wns_b, rt_b, up_b = parse_cpd_wns("control_B.log")
    deltas_b = parse_deltas("control_B.log")
    non_zero_b = [d for d in deltas_b if abs(d) > 1e-9]
    report.append(f"### Case B (Control)")
    report.append(f"- CPD: {cpd_b} ns")
    report.append(f"- WNS: {wns_b} ns")
    report.append(f"- Runtime: {rt_b} s")
    report.append(f"- Injection Updates: {len(deltas_b)}")
    report.append(f"- Non-zero deltas: {len(non_zero_b)}")
    
    # Case C
    cpd_c, wns_c, rt_c, up_c = parse_cpd_wns("replace_C.log")
    deltas_c = parse_deltas("replace_C.log")
    non_zero_c = [d for d in deltas_c if abs(d) > 1e-9]
    report.append(f"### Case C (Replace)")
    report.append(f"- CPD: {cpd_c} ns")
    report.append(f"- WNS: {wns_c} ns")
    report.append(f"- Runtime: {rt_c} s")
    report.append(f"- Injection Updates: {len(deltas_c)}")
    report.append(f"- Non-zero deltas: {len(non_zero_c)}")

    # Part C
    report.append("## Part C: Step 3.1 Cadence-Replay Validation")
    
    # Record
    cpd_rec, wns_rec, rt_rec, up_rec = parse_cpd_wns("record_3_1.log")
    ev_rec, _, _, h_rec = parse_schedule("record_3_1.log", "record")
    report.append(f"### Run 1 (Baseline Record)")
    report.append(f"- CPD: {cpd_rec} ns")
    report.append(f"- Recorded Events: {ev_rec}")
    report.append(f"- Schedule Hash: {h_rec}")
    
    # Replay
    cpd_rep, wns_rep, rt_rep, up_rep = parse_cpd_wns("replay_3_1.log")
    ev_rep, exec_rep, miss_rep, h_rep = parse_schedule("replay_3_1.log", "replay")
    report.append(f"### Run 2 (Replace Replay)")
    report.append(f"- CPD: {cpd_rep} ns")
    report.append(f"- Scheduled Events: {ev_rep}")
    report.append(f"- Executed Events: {exec_rep}")
    report.append(f"- Missed Events: {miss_rep}")
    report.append(f"- Replay Hash: {h_rep}")
    
    # Compliance Check
    if ev_rec and ev_rep and ev_rec == ev_rep and exec_rep == ev_rep and miss_rep == 0:
        report.append("\n**Replay Compliance: PASS**")
    elif ev_rec is None:
        report.append("\n**Replay Compliance: FAIL (Record missing)**")
    else:
        report.append(f"\n**Replay Compliance: FAIL (Scheduled={ev_rep}, Executed={exec_rep}, Missed={miss_rep})**")
        
    print("\n".join(report))

if __name__ == "__main__":
    main()
