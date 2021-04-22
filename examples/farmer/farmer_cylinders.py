# Copyright 2020 by B. Knueven, D. Mildebrath, C. Muir, J-P Watson, and D.L. Woodruff
# This software is distributed under the 3-clause BSD License.
# general example driver for farmer with cylinders

import farmer
import mpisppy.cylinders

# Make it all go
from mpisppy.utils.sputils import spin_the_wheel
from mpisppy.utils import baseparsers
from mpisppy.utils import vanilla

from mpisppy.extensions.norm_rho_updater import NormRhoUpdater
from mpisppy.convergers.norm_rho_converger import NormRhoConverger

def _parse_args():
    parser = baseparsers.make_parser(num_scens_reqd=True)
    parser = baseparsers.two_sided_args(parser)
    parser = baseparsers.xhatlooper_args(parser)
    parser = baseparsers.fwph_args(parser)
    parser = baseparsers.lagrangian_args(parser)
    parser = baseparsers.xhatshuffle_args(parser)
    parser.add_argument("--crops-mult",
                        help="There will be 3x this many crops (default 1)",
                        dest="crops_mult",
                        type=int,
                        default=1)                
    parser.add_argument("--use-norm-rho-updater",
                        help="Use the norm rho updater extension",
                        dest="use_norm_rho_updater",
                        action="store_true")
    args = parser.parse_args()
    return args


def main():
    
    args = _parse_args()

    num_scen = args.num_scens
    crops_multiplier = args.crops_mult
    
    rho_setter = farmer._rho_setter if hasattr(farmer, '_rho_setter') else None
    if args.default_rho is None and rho_setter is None:
        raise RuntimeError("No rho_setter so there must be --default-rho") 
    
    scenario_creator = farmer.scenario_creator
    scenario_denouement = farmer.scenario_denouement
    all_scenario_names = ['scen{}'.format(sn) for sn in range(num_scen)]
    scenario_creator_kwargs = {
        'use_integer': False,
        "crops_multiplier": crops_multiplier,
    }
    scenario_names = [f"Scenario{i+1}" for i in range(num_scen)]

    # Things needed for vanilla cylinders
    beans = (args, scenario_creator, scenario_denouement, all_scenario_names)
    
    # Vanilla PH hub
    hub_dict = vanilla.ph_hub(*beans,
                              scenario_creator_kwargs=scenario_creator_kwargs,
                              ph_extensions=None,
                              rho_setter = rho_setter)

    ## hack in adaptive rho
    if args.use_norm_rho_updater:
        hub_dict['opt_kwargs']['PH_extensions'] = NormRhoUpdater
        hub_dict['opt_kwargs']['PHoptions']['norm_rho_options'] = {'verbose': True}

    # FWPH spoke
    if args.with_fwph:
        fw_spoke = vanilla.fwph_spoke(*beans, scenario_creator_kwargs=scenario_creator_kwargs)

    # Standard Lagrangian bound spoke
    if args.with_lagrangian:
        lagrangian_spoke = vanilla.lagrangian_spoke(*beans,
                                              scenario_creator_kwargs=scenario_creator_kwargs,
                                              rho_setter = rho_setter)

    # xhat looper bound spoke
    if args.with_xhatlooper:
        xhatlooper_spoke = vanilla.xhatlooper_spoke(*beans, scenario_creator_kwargs=scenario_creator_kwargs)

    # xhat shuffle bound spoke
    if args.with_xhatshuffle:
        xhatshuffle_spoke = vanilla.xhatshuffle_spoke(*beans, scenario_creator_kwargs=scenario_creator_kwargs)

        
    list_of_spoke_dict = list()
    if args.with_fwph:
        list_of_spoke_dict.append(fw_spoke)
    if args.with_lagrangian:
        list_of_spoke_dict.append(lagrangian_spoke)
    if args.with_xhatlooper:
        list_of_spoke_dict.append(xhatlooper_spoke)
    if args.with_xhatshuffle:
        list_of_spoke_dict.append(xhatshuffle_spoke)

    mpisppy.cylinders.SPOKE_SLEEP_TIME = 0.1

    if True:
        print("ASYNC")
        from mpisppy.cylinders.hub import APHHub
        from mpisppy.opt.aph import APH        
        hub_dict['opt_kwargs']['PHoptions']["async_frac_needed"] = 1.0
        hub_dict['opt_kwargs']['PHoptions']["dispatch_frac"] = 0.33333333333
        hub_dict['opt_kwargs']['PHoptions']["APHnu"] = 1.0
        hub_dict['opt_kwargs']['PHoptions']["APHgamma"] = 1.0
        hub_dict['opt_kwargs']['PHoptions']["async_sleep_secs"] = 0.01
        hub_dict['opt_kwargs']['PHoptions']["PHIterLimit"] = 100
        hub_dict['opt_kwargs']['PHoptions']["bundles_per_rank"] = 0

        hub_dict['hub_kwargs']["options"]["abs_gap"] = 1
        hub_dict['hub_kwargs']["options"]["rel_gap"] = 0.0001        

        hub_dict['hub_class'] = APHHub
        hub_dict['opt_class'] = APH

        hub_dict["opt_kwargs"] = {
            "PHoptions": hub_dict['opt_kwargs']['PHoptions'],
            "all_scenario_names": all_scenario_names,
            "scenario_creator": scenario_creator,
            'scenario_denouement': scenario_denouement,
            "scenario_creator_kwargs": scenario_creator_kwargs
            }

        print("HUB DICT:",hub_dict)

    spin_the_wheel(hub_dict, list_of_spoke_dict)


if __name__ == "__main__":
    main()
