import os
import re
import glob

def get_cpd(log_file):
    if not os.path.exists(log_file): return 99999.0
    with open(log_file, "r") as f:
        content = f.read()
        m = re.search(r"Final critical path delay.*: ([\d\.]+) ns", content)
        if m:
            return float(m.group(1))
    return 99999.0

best_cpd = 99999.0
best_config = ""

# Check all stereo directories
dirs = glob.glob("*stereo*prob*")
for d in dirs:
    log = os.path.join(d, "vpr.log")
    cpd = get_cpd(log)
    if cpd < best_cpd:
        best_cpd = cpd
        best_config = d

print(f"Best Config: {best_config}")
print(f"Best CPD: {best_cpd}")
