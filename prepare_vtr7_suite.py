import subprocess
import os

vtr_7_benchmarks = [
    "bgm", "blob_merge", "boundtop", "ch_intrinsics", "diffeq1", "diffeq2",
    "LU8PEEng", "LU32PEEng", "mcml", "mkDelayWorker32B", "mkPktMerge",
    "mkSMAdapter4B", "or1200", "raygentop", "sha", "stereovision0",
    "stereovision1", "stereovision2", "stereovision3"
]

arch = "./vtr_flow/arch/timing/k6_frac_N10_mem32K_40nm.xml"
vtr_flow_script = "vtr_flow/scripts/run_vtr_flow.py"

# Create output directory for BLIFs
os.makedirs("vtr7_blifs", exist_ok=True)

for bench in vtr_7_benchmarks:
    verilog_path = f"vtr_flow/benchmarks/verilog/{bench}.v"
    # Special cases if any
    if not os.path.exists(verilog_path):
        # Search for it
        print(f"Searching for {bench}.v...")
        search = subprocess.check_output(f"find vtr_flow/benchmarks/ -name {bench}.v", shell=True).decode().strip().split('\n')[0]
        if search:
            verilog_path = search
        else:
            print(f"Warning: could not find {bench}.v")
            continue

    print(f"Synthesizing {bench}...")
    temp_dir = f"temp_synth_{bench}"
    cmd = [
        "python3", vtr_flow_script,
        verilog_path, arch,
        "-end", "abc",
        "-temp_dir", temp_dir
    ]
    
    subprocess.run(cmd)
    
    # Copy the resulting BLIF to central folder
    # Usually named {bench}.pre-vpr.blif or similar in temp_dir
    blif_src = os.path.join(temp_dir, f"{bench}.pre-vpr.blif")
    if not os.path.exists(blif_src):
        # Try alternate name
        blif_src = os.path.join(temp_dir, f"{bench}.abc.blif")
    
    if os.path.exists(blif_src):
        subprocess.run(["cp", blif_src, f"vtr7_blifs/{bench}.blif"])
        print(f"Successfully synthesized {bench}")
    else:
        print(f"Error: Synthesis failed for {bench}")
    
    # Clean up temp dir
    subprocess.run(["rm", "-rf", temp_dir])

print("Synthesis wave complete.")
