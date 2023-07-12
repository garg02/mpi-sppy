import subprocess
import pandas as pd
import os
import json
import matplotlib.pyplot as plt

# script to run the farmer_perturb_PH.py script for different values of total_perturb
# and save the results in a csv file. Also, plot the relationship between the objective

num_scens = 3
max_iterations = 500
solver_name = "gurobi_persistent"
default_rho = 1
perturb_range = (-0.20, 0.20)
perturb_step = 0.01

for perturb in range(int(perturb_range[0] / perturb_step), int(perturb_range[1] / perturb_step) + 1):
    total_perturb = perturb * perturb_step

    cmd = f"python farmer_perturb_PH.py --num-scens {num_scens} --max-iterations {max_iterations} --solver-name {solver_name} --default-rho {default_rho} --total-perturb {total_perturb}"
    subprocess.run(cmd, shell=True)

results_dir = "results"
result_files = os.listdir(results_dir)

data = []
for file in result_files:
    if file.endswith(".json"):
        file_path = os.path.join(results_dir, file)
        with open(file_path, "r") as f:
            result_data = json.load(f)
            data.append(result_data)

df = pd.DataFrame(data)
df.to_csv("results.csv", index=False)

plt.figure(figsize=(8, 6))
plt.scatter(df["total_perturb"], df["obj"])
plt.xlabel("Total Perturb")
plt.ylabel("Objective Value")
plt.title("Relationship between Obj and Total Perturb")
plt.grid(True)
plt.savefig("obj_vs_total_perturb.png")
plt.show()