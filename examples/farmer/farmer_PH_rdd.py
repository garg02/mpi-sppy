import farmer
from mpisppy.opt.ph import PH
from mpisppy.utils import config
import json
import os
from datetime import datetime


def _parse_args():
    # create a config object and parse
    cfg = config.Config()
    
    cfg.num_scens_required()
    cfg.popular_args()
    cfg.two_sided_args()
    cfg.ph_args()
    cfg.add_to_config("crops_mult",
                         description="There will be 3x this many crops (default 1)",
                         domain=int,
                         default=1)
    cfg.add_to_config("total_perturb",
                         description="total yields will be perturbed by this factor (default 0)",
                         domain=float,
                         default=0)
    cfg.add_to_config("rel_perturb",
                         description="rel. yields will be perturbed by this factor (default 0)",
                         domain=float,
                         default=0)
    cfg.add_to_config("use_integer",
                         description="If True, restricts variables to be integer (default false)",
                         domain=bool,
                         default=False)
    
    cfg.parse_command_line("farmer_PH_rdd")
    return cfg


def main():
    
    cfg = _parse_args()

    num_scen = cfg.num_scens
    crops_multiplier = cfg.crops_mult
    total_perturb = cfg.total_perturb
    rel_perturb = cfg.rel_perturb
    use_integer = cfg.use_integer

    rho_setter = farmer._rho_setter if hasattr(farmer, '_rho_setter') else None
    if cfg.default_rho is None and rho_setter is None:
        raise RuntimeError("No rho_setter so a default must be specified via --default-rho")
    
    scenario_creator = farmer.scenario_creator
    scenario_denouement = farmer.scenario_denouement

    all_scenario_names = ['scen{}'.format(sn) for sn in range(num_scen)]
    scenario_creator_kwargs = {
        'use_integer': use_integer,
        "crops_multiplier": crops_multiplier,
        "total_perturb": total_perturb,
        "rel_perturb": rel_perturb,
    }

    options = {
        "solver_name": cfg.solver_name,
        "PHIterLimit": cfg.max_iterations,
        "defaultPHrho": cfg.default_rho,
        "convthresh": 1e-7,
        "verbose": cfg.verbose,
        "display_progress": cfg.display_progress,
        "display_timing": cfg.display_convergence_detail,
        "iter0_solver_options": dict(),
        "iterk_solver_options": dict(),
    }

    ph = PH(
        options,
        all_scenario_names,
        scenario_creator,
        scenario_creator_kwargs=scenario_creator_kwargs,
        scenario_denouement=scenario_denouement
    )

    _, obj, _ = ph.ph_main()

    # Create a results folder if it doesn't exist
    if not os.path.exists("results"):
        os.makedirs("results")

    # Generate a unique filename using the current timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    date = timestamp.split(" ")[0]
    time = timestamp.split(" ")[1]
    filename = f"results/run_{timestamp}.json"

    # Prepare the data to be saved in the JSON file
    result_data = {
        "date": date,
        "time": time,
        "num_scen": num_scen,
        "crops_multiplier": crops_multiplier,
        "total_perturb": total_perturb,
        "rel_perturb": rel_perturb,
        "use_integer": use_integer,
        "solver_name": cfg.solver_name,
        "max_iterations": cfg.max_iterations,
        "default_rho": cfg.default_rho,
        "obj": obj
    }

    # Save the data as a JSON file
    with open(filename, "w") as file:
        json.dump(result_data, file)

    print(f"Results saved in {filename}")
    
    # if we perturb relative yields in the future, can access the random yields
    # for sname,s in ph.local_scenarios.items():
    #     print(f'Yields for scenario {sname} are as follows:')
    #     for crop in s.CROPS:
    #         print(f'{crop}: {s.Yield[crop].value}')

if __name__ == "__main__":
    main()