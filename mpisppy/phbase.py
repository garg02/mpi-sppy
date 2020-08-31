# This software is distributed under the 3-clause BSD License.
''' PH Base and utility functions - March 2020 modifications to support spbase.py

    Based on mpi4py (but should run with, or without, mpi)
    EVERY INDEX IS ZERO-BASED! (Except stages, which are one based).

    Node names other than ROOT, although strings, must be a number or end in a
    number because mpi4py comms need a number.
    PH using a smart referencemodel that knows how to make its own tree nodes
    and just wants a trailing number in the scenario name.
    Assume we have only non-leaf nodes.

    To check for rank 0 use self.rank == self.rank0
'''
import inspect
import numpy as np
import collections
import mpi4py.MPI as mpi
import time
import datetime as dt
import pyomo.environ as pyo
import mpisppy.utils.listener_util.listener_util as listener_util
import mpisppy.spbase
import logging
import mpisppy.utils.sputils as sputils

from pyomo.opt import SolverFactory, SolverStatus, TerminationCondition
from pyomo.pysp.phutils import find_active_objective

from mpi4py import MPI
from mpisppy import tt_timer

# decorator snarfed from stack overflow - allows per-rank profile output file generation.
def profile(filename=None, comm=mpi.COMM_WORLD):
    pass

logger = logging.getLogger('PHBase')
logger.setLevel(logging.WARN)

class PHBase(mpisppy.spbase.SPBase):
    """
        Args:
            PHoptions (dict): PH options
            all_scenario_names (list): all scenario names
            scenario_creator (fct): returns a concrete model with special things
            scenario_denouement (fct): for post processing and reporting
            all_nodenames (list): all node names; can be None for 2 Stage
            mpicomm (MPI comm): if not given, use the global fullcomm
            rank0 (int): The lowest global rank for this type of object
            cb_data (any): passed directly to the scenario callback
            PH_extensions (object) : optional
            PH_extension_kwargs (keyword arguments): optional,
            PH_converger (object) : optional
            rho_setter (fct): optional


        Attributes:
          local_scenarios (dict of scenario objects): concrete models with 
                extra data, key is name
          comms (dict): keys are node names values are comm objects.
          local_scenario_names (list): names of locals 
          current_solver_options (dict): from PHoptions, but callbacks might change

    """
    def __init__(self, PHoptions, all_scenario_names, scenario_creator,
                 scenario_denouement=None, all_nodenames=None,
                 mpicomm=None, rank0=0, cb_data=None,
                 PH_extensions=None, PH_extension_kwargs=None,
                 PH_converger=None, rho_setter=None):
        super().__init__(PHoptions,
                         all_scenario_names,
                         scenario_creator,
                         scenario_denouement=scenario_denouement,
                         all_nodenames=all_nodenames,
                         mpicomm=mpicomm,
                         rank0=rank0,
                         cb_data=cb_data)

        if self.rank_global == 0:
            tt_timer.toc("Start PHBase.__init__", delta=False)

        # Note that options can be manipulated from outside on-the-fly.
        # self.options (from super) will archive the original options.
        self.PHoptions = PHoptions
        self.options_check()
        self.PH_extensions = PH_extensions
        self.PH_extension_kwargs = PH_extension_kwargs 
        self.PH_converger = PH_converger
        self.rho_setter = rho_setter

        self.iter0_solver_options = PHoptions["iter0_solver_options"]
        self.iterk_solver_options = PHoptions["iterk_solver_options"]
        # flags to complete the invariant
        self.W_disabled = None   # will be set by Prep
        self.prox_disabled = None
        self.convobject = None  # PH converger
        self.attach_xbars()

    def Compute_Xbar(self, verbose=False, synchronizer=None):
        """Gather xbar and x squared bar for each node represented in the list
           and also distribute the values back to the scenarios.

        Args:
          verbose (boolean): verbose output
          synchronizer (object): for asynchronous PH operation

        Note: 
          each scenario knows its own probability and its nodes.
        Note:
          The scenario only "sends a reduce" to its own node's comms so even 
          though the rank is a member of many comms, the scenario won't 
          contribute to the wrong node.
        Note:
          As of March 2019, we concatenate xbar and xsqbar into one long
          vector to make it easier to use the current asynch code.
        """

        nodenames = list() # to transmit to comms
        # This "onlyreduce" thing is done for the sake of asynchronous only
        local_concats = {"OnlyReduce": dict()} # keys are tree node names
        node_concats =  {"OnlyReduce": dict()} # values are concat of xbar 
                                               # and xsqbar
        
        # we need to accumulate all local contributions before the reduce
        for k,s in self.local_scenarios.items():
            nlens = s._PySP_nlens        
            for node in s._PySPnode_list:
                if node.name not in nodenames:
                    ndn = node.name
                    nodenames.append(ndn)
                    mylen = 2*nlens[ndn]
                    if synchronizer is not None and node.name == "ROOT":
                        mylen += self.n_proc
                    local_concats["OnlyReduce"][ndn] = np.zeros(mylen, dtype='d')
                    node_concats["OnlyReduce"][ndn] = np.zeros(mylen, dtype='d')

        # compute the local xbar and sqbar (put the sq in the 2nd 1/2 of concat)
        for k,s in self.local_scenarios.items():
            nlens = s._PySP_nlens        
            for node in s._PySPnode_list:
                ndn = node.name
                for i, v in enumerate(node.nonant_vardata_list):
                    local_concats["OnlyReduce"][ndn][i] += \
                        (s.PySP_prob / node.cond_prob) * v._value
                    local_concats["OnlyReduce"][ndn][nlens[ndn]+i] += \
                        (s.PySP_prob / node.cond_prob) * v._value * v._value

        # compute node xbar values(reduction)
        if synchronizer is None:
            for nodename in nodenames:
                self.comms[nodename].Allreduce(
                    [local_concats["OnlyReduce"][nodename], mpi.DOUBLE],
                    [node_concats["OnlyReduce"][nodename], mpi.DOUBLE],
                    op=mpi.SUM)
        else: # async
            secs_sofar = (dt.datetime.now() - self.startdt).total_seconds()
            # only this rank puts a time for this rank, so the sum is a report
            local_concats["OnlyReduce"]["ROOT"][2*nlens["ROOT"]+self.rank] \
                = secs_sofar
            
            logger.debug('xbar secs_sofar {} on rank {}'.format(secs_sofar, self.rank))
            ###logger.debug('   xbar on rank {}; local_concats {}'.format(self.rank, str(local_concats)))
            synchronizer.compute_global_data(local_concats, node_concats)

            # See if we have enough xbars to proceed (need not be perfect)
            # NOTE: so, for now we start xbarin at one because we are in...
            xbarin = 0 # count ranks (close enough to be a proxy for scenarios)
            while synchronizer.global_quitting == 0: # there is also a break
                for cr in range(self.n_proc):
                    logger.debug('cr {} on rank {} time {}'.\
                        format(cr, self.rank,
                        node_concats["OnlyReduce"]["ROOT"][2*nlens["ROOT"]+cr]))
                    if  node_concats["OnlyReduce"]["ROOT"][2*nlens["ROOT"]+cr] \
                        >= secs_sofar:
                        xbarin += 1
                        logger.debug('xbarin {} on rank {}'.\
                                      format(xbarin, self.rank))
                if xbarin/self.n_proc >= self.PHoptions["async_frac_needed"]:
                    logger.debug('   break on rank {}'.format(self.rank))
                    if verbose and self.rank == self.rank0:
                        print ("(%d)" % xbarin)
                    break   # leave outer while True loop
                else:
                    logger.debug('   sleep on rank {}'.format(self.rank))
                    if verbose and self.rank == self.rank0:
                        print ('s'),
                    time.sleep(self.PHoptions["async_sleep_secs"])
                    synchronizer.get_global_data(node_concats)
                    xbarin = 0 # global_data should have been upated
        
        # set the xbar and xsqbar in all the scenarios
        for k,s in self.local_scenarios.items():
            logger.debug('  top of assign xbar loop for {} on rank {}'.\
                         format(k, self.rank))
            
            nlens = s._PySP_nlens        
            for node in s._PySPnode_list:
                ndn = node.name
                for i in range(nlens[ndn]):
                    s._xbars[(ndn,i)]._value = node_concats["OnlyReduce"][ndn][i]
                    s._xsqbars[(ndn,i)]._value \
                        = node_concats["OnlyReduce"][ndn][nlens[ndn]+i]
                    if verbose and self.rank == self.rank0:
                        print ("rank, scen, node, var, xbar:",
                               self.rank, k, ndn, node.nonant_vardata_list[i].name,
                               pyo.value(s._xbars[(ndn,i)]))


    def Update_W(self, verbose):
        # Assumes the scenarios are up to date
        for k,s in self.local_scenarios.items():
            for ndn_i, nonant in s._nonant_indexes.items():
                xdiff = nonant._value \
                        - s._xbars[ndn_i]._value
                s._Ws[ndn_i]._value += pyo.value(s._PHrho[ndn_i]) * xdiff
                if verbose and self.rank == self.rank0:
                    print ("rank, node, scen, var, W", ndn_i[0], k,
                           self.rank, nonant.name,
                           pyo.value(s._Ws[ndn_i]))

    def convergence_diff(self):
        """ Assumes the scenarios are up to date
            Returns the convergence metric ||x_s - \bar{x}||_1 / num_scenarios
            Note: every scenario has its own node list, with a vardata list
        """

        global_diff = np.zeros(1)
        local_diff = np.zeros(1)
        varcount = 0
        for k,s in self.local_scenarios.items():
            for ndn_i, nonant in s._nonant_indexes.items():
                xval = nonant._value
                xdiff = xval - s._xbars[ndn_i]._value
                local_diff[0] += abs(xdiff)
                varcount += 1
        local_diff[0] /= varcount

        self.comms["ROOT"].Allreduce(local_diff, global_diff, op=mpi.SUM)

        return global_diff[0] / self.n_proc
           

    def Eobjective(self, verbose=False):
        """ Compute the expected objective function with whatever is there.
        NOTE: 
            Assumes the optimization is done beforehand,
            therefore DOES NOT CHECK FEASIBILITY or NON-ANTICIPATIVITY!!
            Uses whatever objective function is there.

        Args:
            ignore_saved_objs (boolean): assume no saved_objs
            verbose (boolean): controls debugging output

        Returns:
            Eobj (float): the expected objective function value
        """
        local_Eobj = np.zeros(1)
        global_Eobj = np.zeros(1)
        for k,s in self.local_scenarios.items():
            if self.bundling:
                objfct = self.saved_objs[k]
            else:
                objfct = find_active_objective(s, True)
            local_Eobj[0] += s.PySP_prob * pyo.value(objfct)
            if verbose:
                print ("caller", inspect.stack()[1][3])
                print ("E_Obj Scenario {}, prob={}, Obj={}, ObjExpr={}"\
                       .format(k, s.PySP_prob, pyo.value(objfct), objfct.expr))

        self.mpicomm.Allreduce(local_Eobj, global_Eobj, op=mpi.SUM)

        return global_Eobj[0]

    def Ebound(self, verbose=False):
        """ Compute the expected bound.
        NOTE: 
            Assumes the optimization is done beforehand.
            Uses whatever bound is attached to the sub-problems.
        Args:
            verbose (boolean): controls debugging output

        Returns:
            Eobj (float): the expected objective function value
        """
        local_Ebound = np.zeros(1)
        global_Ebound = np.zeros(1)

        for k,s in self.local_subproblems.items():
            logger.debug("  in loop Ebound k={}, rank={}".format(k, self.rank))
            local_Ebound[0] += s.PySP_prob * s._PySP_ob
            if verbose:
                print ("caller", inspect.stack()[1][3])
                print ("E_Bound Scenario {}, prob={}, bound={}"\
                       .format(k, s.PySP_prob, s._PySP_ob))
        
        self.mpicomm.Allreduce(local_Ebound, global_Ebound, op=mpi.SUM)

        return global_Ebound[0]

    def avg_min_max(self, compstr):
        """ Can be used to track convergence progress.
        Args:
            compstr (str): the name of the Pyomo component. Should not
                           be indexed.
        Returns:
            (avg, min, max) (float): taken across all scenarios
        NOTE:
            Not user-friendly. If you give a bad compstr, it will just crash.
        """
        firsttime = True
        localavg = np.zeros(1, dtype='d')
        localmin = np.zeros(1, dtype='d')
        localmax = np.zeros(1, dtype='d')
        globalavg = np.zeros(1, dtype='d')
        globalmin = np.zeros(1, dtype='d')
        globalmax = np.zeros(1, dtype='d')

        v_cuid = pyo.ComponentUID(compstr)

        for k,s in self.local_scenarios.items():

            compv = pyo.value(v_cuid.find_component_on(s))

            
            ###compv = pyo.value(getattr(s, compstr))
            localavg[0] += s.PySP_prob * compv  
            if compv < localmin[0] or firsttime:
                localmin[0] = compv
            if compv > localmax[0] or firsttime:
                localmax[0] = compv
            firsttime = False

        self.comms["ROOT"].Allreduce([localavg, mpi.DOUBLE],
                                     [globalavg, mpi.DOUBLE],
                                     op=mpi.SUM)
        self.comms["ROOT"].Allreduce([localmin, mpi.DOUBLE],
                                     [globalmin, mpi.DOUBLE],
                                     op=mpi.MIN)
        self.comms["ROOT"].Allreduce([localmax, mpi.DOUBLE],
                                     [globalmax, mpi.DOUBLE],
                                     op=mpi.MAX)
        return (float(globalavg[0]),
                float(globalmin[0]),
                float(globalmax[0]))

    def _save_original_nonants(self):
        for k,s in self.local_scenarios.items():
            if hasattr(s,"_PySP_original_fixedness"):
                print ("ERROR: Attempt to replace original nonants")
                raise
            nlens = s._PySP_nlens
            if s._PySP_nonant_cache is None:
                clen = sum(nlens[ndn] for ndn in nlens)
                s._PySP_original_fixedness = [None] * clen
                s._PySP_original_nonants = np.zeros(clen, dtype='d')

            for ci, xvar in enumerate(s._nonant_indexes.values()):
                s._PySP_original_fixedness[ci]  = xvar.is_fixed()
                s._PySP_original_nonants[ci]  = xvar._value

    def _restore_original_nonants(self):
        """ Restore the saved values and fix status 
            of the Vars subject to non-anticipativity.
            Loop over the scenarios to restore, but loop over subproblems
            to alert persisten solvers.

        WARNING: 
            We are counting on Pyomo indexes not to change order between save
            and restoration. THIS WILL NOT WORK ON BUNDLES (Feb 2019) but
            hopefully does not need to.
        """
        for k,s in self.local_scenarios.items():

            persistent_solver = None
            if not self.bundling:
                if (sputils.is_persistent(s._solver_plugin)):
                    persistent_solver = s._solver_plugin
            else:
                print("restore_original_nonants called for a bundle")
                raise

            for ci, vardata in enumerate(s._nonant_indexes.values()):
                vardata._value = s._PySP_original_nonants[ci]
                vardata.fixed = s._PySP_original_fixedness[ci]
                if persistent_solver != None:
                    persistent_solver.update_var(vardata)

    def _save_nonants(self):
        """ Save the values and fixedness status of the Vars that are
            subject to non-anticipativity.
        Args:
        NOTE:
            Assumes _PySP_nonant_cache is on the scenarios and can be used
            as a list. 
        WARNING: We are counting on Pyomo indexes not to change order before
        the restoration. We also need the Var type to remain stable.
        NOTE: the value cache is np because it might be transmitted
        """
        for k,s in self.local_scenarios.items():
            nlens = s._PySP_nlens
            if s._PySP_nonant_cache is None:
                clen = sum(nlens[ndn] for ndn in nlens)
                s._PySP_nonant_cache = np.zeros(clen, dtype='d')
                s._PySP_fixedness_cache = [None for _ in range(clen)]

            for ci, xvar in enumerate(s._nonant_indexes.values()):
                s._PySP_nonant_cache[ci]  = xvar._value
                s._PySP_fixedness_cache[ci]  = xvar.is_fixed()

    def _restore_nonants(self):
        """ Restore the saved values and fix status 
            of the Vars subject to non-anticipativity.
            Loop over the scenarios to restore, but loop over subproblems
            to alert persisten solvers.

        WARNING: 
            We are counting on Pyomo indexes not to change order between save
            and restoration.
        """
        for k,s in self.local_scenarios.items():

            persistent_solver = None
            if not self.bundling:
                if (sputils.is_persistent(s._solver_plugin)):
                    persistent_solver = s._solver_plugin

            for ci, vardata in enumerate(s._nonant_indexes.values()):
                vardata._value = s._PySP_nonant_cache[ci]
                vardata.fixed = s._PySP_fixedness_cache[ci]

                if not self.bundling and persistent_solver is not None:
                    persistent_solver.update_var(vardata)


        if self.bundling:  # we might need to update subproblem peristent solvers
            rank_local = self.rank
            for k,s in self.local_subproblems.items():
                if (sputils.is_persistent(s._solver_plugin)):
                    persistent_solver = s._solver_plugin
                else:
                    break  # all solvers should be the same

                # the bundle number is the last number in the name
                bunnum = sputils.extract_num(k)
                # for the scenarios in this bundle, update Vars
                for sname, scen in self.local_scenarios.items():
                    if sname not in self.names_in_bundles[rank_local][bunnum]:
                        break
                    for vardata in scen._nonant_indexes.values():
                        persistent_solver.update_var(vardata)

    def _fix_nonants(self, cache):
        """ Fix the Vars subject to non-anticipativity at given values.
            Loop over the scenarios to restore, but loop over subproblems
            to alert persistent solvers.
        Args:
            cache (ndn dict of list or numpy vector): values at which to fix
        WARNING: 
            We are counting on Pyomo indexes not to change order between
            when the cache_list is created and used.
        NOTE:
            You probably want to call _save_nonants right before calling this
        """
        for k,s in self.local_scenarios.items():

            persistent_solver = None
            if not self.bundling:
                if (sputils.is_persistent(s._solver_plugin)):
                    persistent_solver = s._solver_plugin

            nlens = s._PySP_nlens
            for node in s._PySPnode_list:
                ndn = node.name
                if ndn not in cache:
                    raise RuntimeError("Could not find {} in {}"\
                                       .format(ndn, cache))
                if cache[ndn] is None:
                    raise RuntimeError("Empty cache for scen={}, node={}"                                       .format(k, ndn))         
                if len(cache[ndn]) != nlens[ndn]:
                    raise RuntimeError("Needed {} nonant Vars for {}, got {}"\
                                       .format(nlens[ndn], ndn, len(cache[ndn])))
                for i in range(nlens[ndn]): 
                    this_vardata = node.nonant_vardata_list[i]
                    this_vardata._value = cache[ndn][i]
                    this_vardata.fix()
                    if not self.bundling and persistent_solver is not None:
                        persistent_solver.update_var(this_vardata)

        if self.bundling:  # we might need to update persistent solvers
            rank_local = self.rank
            for k,s in self.local_subproblems.items():
                if (sputils.is_persistent(s._solver_plugin)):
                    persistent_solver = s._solver_plugin
                else:
                    break  # all solvers should be the same

                # the bundle number is the last number in the name
                bunnum = sputils.extract_num(k)
                # for the scenarios in this bundle, update Vars
                for sname, scen in self.local_scenarios.items():
                    if sname not in self.names_in_bundles[rank_local][bunnum]:
                        break
                    nlens = scen._PySP_nlens
                    for node in scen._PySPnode_list:
                        for i in range(nlens[node.name]):
                            this_vardata = node.nonant_vardata_list[i]
                            persistent_solver.update_var(this_vardata)

                            
    def _restore_original_fixedness(self):
        # We are going to hack a little to get the original fixedness, but current values
        # (We are assuming that algorithms are not fixing anticipative vars; but if they
        # do, they had better put their fixedness back to its correct state.)
        self._save_nonants()
        for k,s in self.local_scenarios.items():        
            for ci, _ in enumerate(s._nonant_indexes):
                s._PySP_fixedness_cache[ci] = s._PySP_original_fixedness[ci]
        self._restore_nonants()

        
    def _populate_W_cache(self, cache):
        """ Copy the W values for noants *for all local scenarios*
        Args:
            cache (np vector) to receive the W's for all local scenarios

        NOTE: This is not the same as the nonant Vars because it puts all local W
              values into the same cache and the cache is *not* attached to the scenario.

        """
        ci = 0 # Cache index
        for model in self.local_scenarios.values():
            for ix in model._nonant_indexes:
                assert(ci < len(cache))
                cache[ci] = pyo.value(model._Ws[ix])
                ci += 1

    def _put_nonant_cache(self, cache):
        """ Put the value in the cache for noants *for all local scenarios*
        Args:
            cache (np vector) to receive the nonant's for all local scenarios

        """
        ci = 0 # Cache index
        for model in self.local_scenarios.values():
            for i,_ in enumerate(model._nonant_indexes):
                assert(ci < len(cache))
                model._PySP_nonant_cache[i] = cache[ci]
                ci += 1

    def W_from_flat_list(self, flat_list):
        ''' Set the W values *for all local scenarios* from one list

        WARNING: 
            We are counting on Pyomo indexes not to change order between list creation and use.
        '''
        ci = 0 # Cache index
        for model in self.local_scenarios.values():
            for ndn_i in model._nonant_indexes:
                model._Ws[ndn_i].value = flat_list[ci]
                ci += 1
        
        # That is it, unless we are using a persistent solver
        using_persistent = all(
            sputils.is_persistent(model._solver_plugin)
            for model in self.local_subproblems.values()
        )
        if not using_persistent:
            return

        if self.bundling:
            for (name, model) in self.local_subproblems.items():
                bundle_num = sputils.extract_num(name)
                solver = model._solver_plugin
                for sname in self.names_in_bundles[self.rank][bundle_num]:
                    smodel = self.local_scenarios[sname]
                    for vardata in smodel._nonant_indexes.values():
                        solver.update_var(vardata)
        else:
            for model in self.local_scenarios.values():
                solver = model._solver_plugin
                for vardata in model._nonant_indexes.values():
                    solver.update_var(vardata)

    def _update_E1(self):
        """ Add up the probabilities of all scenarios using a reduce call.
            then attach it to the PH object as a float.
        """
        localP = np.zeros(1, dtype='d')
        globalP = np.zeros(1, dtype='d')

        for k,s in self.local_scenarios.items():
            localP[0] +=  s.PySP_prob

        self.mpicomm.Allreduce([localP, mpi.DOUBLE],
                           [globalP, mpi.DOUBLE],
                           op=mpi.SUM)

        self.E1 = float(globalP[0])

    def feas_prob(self):
        """ Check for feas of all scenarios using a reduction.
        Assumes the scenarios have a boolean _PySP_feas_indicator.

        Returns:
            Sum of Feas Prob (is == E1 if all feasible)
        WARNING: assumes that the _PySP_feas_indicator has been set.
        """

        # locals[0] is E_feas and locals[1] is E_1
        locals = np.zeros(1, dtype='d')
        globals = np.zeros(1, dtype='d')

        for k,s in self.local_scenarios.items():
            if s._PySP_feas_indicator:
                locals[0] += s.PySP_prob

        self.mpicomm.Allreduce([locals, mpi.DOUBLE],
                           [globals, mpi.DOUBLE],
                           op=mpi.SUM)

        return float(globals[0])

    def _use_rho_setter(self, verbose):
        """ set rho values using a function self.rho_setter
        that gives us a list of (id(vardata), rho)]
        """
        if self.rho_setter is None:
            return
        didit = 0
        skipped = 0
        rho_setter_kwargs = self.PHoptions['rho_setter_kwargs'] \
                            if 'rho_setter_kwargs' in self.PHoptions \
                            else dict()
        for sname, scenario in self.local_scenarios.items():
            rholist = self.rho_setter(scenario, **rho_setter_kwargs)
            for (vid, rho) in rholist:
                try:
                    (ndn, i) = scenario._varid_to_nonant_index[vid]
                    scenario._PHrho[(ndn, i)] = rho
                    didit += 1
                except:
                    skipped += 1
        if verbose and self.rank == self.rank0:
            print ("rho_setter set",didit,"and skipped",skipped)

    def _disable_prox(self):
        self.prox_disabled = True
        for k, scenario in self.local_scenarios.items():
            for (ndn, i) in scenario._nonant_indexes:
                scenario._PHprox_on[(ndn,i)]._value = 0

    def _disable_W_and_prox(self):
        self.prox_disabled = True
        self.W_disabled = True
        for k, scenario in self.local_scenarios.items():
            for (ndn, i) in scenario._nonant_indexes:
                scenario._PHprox_on[(ndn,i)]._value = 0
                scenario._PHW_on[(ndn,i)]._value = 0

    def _disable_W(self):
        # It would be odd to disable W and not prox.
        self.W_disabled = True
        for scenario in self.local_scenarios.values():
            for (ndn, i) in scenario._nonant_indexes:
                scenario._PHW_on[ndn,i]._value = 0

    def _reenable_prox(self):
        self.prox_disabled = False        
        for k, scenario in self.local_scenarios.items():
            for (ndn, i) in scenario._nonant_indexes:
                scenario._PHprox_on[(ndn,i)]._value = 1

    def _reenable_W_and_prox(self):
        self.prox_disabled = False
        self.W_disabled = False
        for k, scenario in self.local_scenarios.items():
            for (ndn, i) in scenario._nonant_indexes:
                scenario._PHprox_on[(ndn,i)]._value = 1
                scenario._PHW_on[(ndn,i)]._value = 1

    def _reenable_W(self):
        self.W_disabled = False
        for k, scenario in self.local_scenarios.items():
            for (ndn, i) in scenario._nonant_indexes:
                scenario._PHW_on[(ndn,i)]._value = 1

    def post_solve_bound(self, solver_options=None, verbose=False):
        '''Compute a bound Lagrangian bound using the existing weights.

        Args:
            verbose (boolean): controls debugging output
            solver_options (dict): options for these solves

        Returns:
            bound (float): an objective function bound

        Notes: This function overwrites current variable values. This
            is only suitable for use at the end of the solves, or if
            you really know what you are doing.  It is not suitable as
            a general, per-iteration Lagrangian bound solver.

        '''
        if (self.rank == self.rank0):
            print('Warning: Lagrangian bounds might not be correct in certain '
                  'cases where there are integers not subject to '
                  'non-anticipativity and those integers do not reach integrality.')
        if (verbose and self.rank == self.rank0):
            print('Beginning post-solve Lagrangian bound computation')

        if (self.W_disabled):
            self._reenable_W()
        self._disable_prox()

        # Fixed variables can lead to an invalid lower bound
        self._restore_original_fixedness()

        # If dis_prox=True, they are enabled at the end, and Ebound returns
        # the incorrect value (unless you explicitly disable them again)
        self.solve_loop(solver_options=solver_options,
                        dis_prox=False, # Important
                        gripe=True, 
                        tee=False,
                        verbose=verbose)

        bound = self.Ebound(verbose)

        # A half-hearted attempt to restore the state
        self._reenable_prox()

        if (verbose and self.rank == self.rank0):
            print(f'Post-solve Lagrangian bound: {bound:.4f}')
        return bound

    def FormEF(self, scen_dict, EF_name=None):
        """ Make the EF for a list of scenarios. Designed with the main use-case
            being bundles.

        Args:
            scen_dict (dict): subset of local_scenarios; the scenarios to put in the EF
            EF_name (string): optional name for the EF

        Returns:
            EF (ConcreteModel): the EF with explicit non-anticipativity constraints
        NOTE: We attach a list of the scenario names called _PySP_subsecen_names
        NOTE:
            We deactivate the objective on the scenarios.
        NOTE:
            The scenarios are sub-blocks, so they naturally get the EF solution
            Also the EF objective references Vars and Parms on the scenarios
            and hence is automatically updated when the scenario
            objectives are. THIS IS ALL CRITICAL to bundles.
            xxxx TBD: ask JP about objective function transmittal to persistent solvers
        NOTE:
            Objectives are scaled (normalized) by PySP_prob
        """
        if len(scen_dict) == 0:
            raise RuntimeError("Empty scenario list for EF")

        if len(scen_dict) == 1:
            sname, scenario_instance = list(scen_dict.items())[0]
            if EF_name is not None:
                print ("WARNING: EF_name="+EF_name+" not used; singleton="+sname)
                print ("MAJOR WARNING: a bundle of size one encountered; if you try to compute bounds it might crash (Feb 2019)")
            return scenario_instance

        # The individual scenario instances are sub-blocks of the binding
        # instance. Needed to facilitate bundles + persistent solvers
        if not hasattr(self, "saved_objs"): # First bundle
             self.saved_objs = dict()

        for sname, scenario_instance in scen_dict.items():
            if sname not in self.local_scenarios:
                raise RuntimeError("EF scen not in local_scenarios="+sname)
            self.saved_objs[sname] = find_active_objective(scenario_instance, True)

        EF_instance = sputils._create_EF_from_scen_dict(scen_dict, EF_name=EF_name,
                        nonant_for_fixed_vars=False)
        return EF_instance

    def solve_one(self, solver_options, k, s,
                  dtiming=False,
                  gripe=False,
                  tee=False,
                  verbose=False,
                  disable_pyomo_signal_handling=False):
        """ Solve one subproblem.

        Args:
            solver_options (dict or None): the scenario solver options
            k (str): subproblem name
            s (ConcreteModel with appendages): the subproblem to solve
            dtiming (boolean): indicates that timing should be reported
            gripe (boolean): output a message if a solve fails
            disable_pyomo_signal_handling (boolean): set to true for asynch, 
                                                     ignored for persistent solvers.
            tee (boolean): show solver output to screen if possible
            verbose (boolean): indicates verbose output
            disable_pyomo_signal_handling(boolean): for system call solvers
        """


        def _vb(msg): 
            if verbose and self.rank == self.rank0:
                print ("(rank0) " + msg)
        
        # if using a persistent solver plugin,
        # re-compile the objective due to changed weights and x-bars
        if (sputils.is_persistent(s._solver_plugin)):
            set_objective_start_time = time.time()

            active_objective_datas = list(s.component_data_objects(
                pyo.Objective, active=True, descend_into=True))
            if len(active_objective_datas) > 1:
                raise RuntimeError('Multiple active objectives identified '
                                   'for scenario {sn}'.format(sn=s._name))
            elif len(active_objective_datas) < 1:
                raise RuntimeError('Could not find any active objectives '
                                   'for scenario {sn}'.format(sn=s._name))
            else:
                s._solver_plugin.set_objective(active_objective_datas[0])

            if dtiming:

                set_objective_time = time.time() - set_objective_start_time

                all_set_objective_times = self.mpicomm.gather(set_objective_time,
                                                          root=0)
                if self.rank == self.rank0:
                    print("Set objective times (seconds):")
                    print("\tmin=%4.2f mean=%4.2f max=%4.2f" %
                          (np.mean(all_set_objective_times),
                           np.mean(all_set_objective_times),
                           np.max(all_set_objective_times)))

        solve_start_time = time.time()
        if (solver_options):
            _vb("Using sub-problem solver options="
                + str(solver_options))
            for option_key,option_value in solver_options.items():
                s._solver_plugin.options[option_key] = option_value

        solve_keyword_args = dict()
        if self.rank == self.rank0:
            if tee is not None and tee is True:
                solve_keyword_args["tee"] = True
        if (sputils.is_persistent(s._solver_plugin)):
            solve_keyword_args["save_results"] = False
        elif disable_pyomo_signal_handling:
            solve_keyword_args["use_signal_handling"] = False

        try:
            results = s._solver_plugin.solve(s,
                                             **solve_keyword_args,
                                             load_solutions=False)
            solve_err = False
        except:
            solve_err = True

        pyomo_solve_time = time.time() - solve_start_time
        if solve_err or (results.solver.status != SolverStatus.ok) \
              or (results.solver.termination_condition \
                    != TerminationCondition.optimal):
             s._PySP_feas_indicator = False

             if gripe:
                 name = self.__class__.__name__
                 if self.spcomm:
                     name = self.spcomm.__class__.__name__
                 print (f"[{name}] Solve failed for scenario {s.name}")
                 if not solve_err:
                     print ("status=", results.solver.status)
                     print ("TerminationCondition=",
                            results.solver.termination_condition)
        else:
            if sputils.is_persistent(s._solver_plugin):
                s._solver_plugin.load_vars()
            else:
                s.solutions.load_from(results)
            if self.is_minimizing:
                s._PySP_ob = results.Problem[0].Lower_bound
            else:
                s._PySP_ob = results.Problem[0].Upper_bound
            s._PySP_feas_indicator = True
        # TBD: get this ready for IPopt (e.g., check feas_prob every time)
        # propogate down
        if hasattr(s,"_PySP_subscen_names"): # must be a bundle
            for sname in s._PySP_subscen_names:
                 self.local_scenarios[sname]._PySP_feas_indicator\
                     = s._PySP_feas_indicator
        return pyomo_solve_time
    
    
    def solve_loop(self, solver_options=None,
                   use_scenarios_not_subproblems=False,
                   dtiming=False,
                   dis_W=False,
                   dis_prox=False,
                   gripe=False,
                   disable_pyomo_signal_handling=False,
                   tee=False,
                   verbose=False):
        """ Loop over self.local_subproblems and solve them in a manner 
            dicated by the arguments. In addition to changing the Var
            values in the scenarios, update _PySP_feas_indictor for each.

        ASSUMES:
            Every scenario already has a _solver_plugin attached.

        Args:
            solver_options (dict or None): the scenario solver options
            use_scenarios_not_subproblems (boolean): for use by bounds
            dtiming (boolean): indicates that timing should be reported
            dis_W (boolean): indicates that W should be disabled (and re-enabled)
            dis_prox (boolean): disable (and re-enable) prox term
            gripe (boolean): output a message if a solve fails
            disable_pyomo_signal_handling (boolean): set to true for asynch, 
                                                     ignored for persistent solvers.
            tee (boolean): show solver output to screen if possible
            verbose (boolean): indicates verbose output

        NOTE: I am not sure what happens with solver_options None for
              a persistent solver. Do options persist?

        NOTE: set_objective takes care of W and prox changes.
        """
        def _vb(msg): 
            if verbose and self.rank == self.rank0:
                print ("(rank0) " + msg)
        _vb("Entering solve_loop function.")
        if dis_W and dis_prox:
            self._disable_W_and_prox()
        elif dis_W:
            self._disable_W()
        elif dis_prox:
            self._disable_prox()
        logger.debug("  early solve_loop for rank={}".format(self.rank))
        # note that when there is no bundling, scenarios are subproblems
        if use_scenarios_not_subproblems:
            s_source = self.local_scenarios
        else:
            s_source = self.local_subproblems
        for k,s in s_source.items():
            logger.debug("  in loop solve_loop k={}, rank={}".format(k, self.rank))
            if tee:
                print(f"Tee solve for {k} on global rank {self.rank_global}")
            pyomo_solve_time = self.solve_one(solver_options, k, s,
                                              dtiming=dtiming,
                                              verbose=verbose,
                                              tee=tee,
                                              gripe=gripe,
                disable_pyomo_signal_handling=disable_pyomo_signal_handling
            )

        if dtiming:
            all_pyomo_solve_times = self.mpicomm.gather(pyomo_solve_time, root=0)
            if self.rank == self.rank0:
                print("Pyomo solve times (seconds):")
                print("\tmin=%4.2f mean=%4.2f max=%4.2f" %
                      (np.min(all_pyomo_solve_times),
                      np.mean(all_pyomo_solve_times),
                      np.max(all_pyomo_solve_times)))

        if dis_W and dis_prox:
            self._reenable_W_and_prox()
        elif dis_W:
            self._reenable_W()
        elif dis_prox:
            self._reenable_prox()



    def attach_Ws_and_prox(self):
        for (sname, scenario) in self.local_scenarios.items():
            # these are bound by index to the vardata list at the node
            scenario._Ws = pyo.Param(scenario._nonant_indexes.keys(),
                                        initialize=0.0,
                                        mutable=True)
            
            # create ph objective terms, but disabled
            scenario._PHW_on = pyo.Param(scenario._nonant_indexes.keys(),
                                        initialize=0.0,
                                        mutable=True)
            self.W_disabled = True
            scenario._PHprox_on = pyo.Param(scenario._nonant_indexes.keys(),
                                        initialize=0.0,
                                        mutable=True)
            self.prox_disabled = True
            # note that rho is per var and scenario here
            scenario._PHrho = pyo.Param(scenario._nonant_indexes.keys(),
                                        mutable=True,
                                        default=self.PHoptions["defaultPHrho"])

    def attach_varid_to_nonant_index(self):
        for (sname, scenario) in self.local_scenarios.items():
            """ In order to support rho setting, create a map
                from the id of vardata object back its _nonant_index.
            """
            scenario._varid_to_nonant_index = {
                id(node.nonant_vardata_list[i]): (node.name, i)
                for node in scenario._PySPnode_list
                for i in range(scenario._PySP_nlens[node.name])}

    def attach_PH_to_objective(self, add_duals=True, add_prox=False):
        for (sname, scenario) in self.local_scenarios.items():
            """Attach the dual and prox terms to the objective.
            """
            if ((not add_duals) and (not add_prox)):
                return
            objfct = find_active_objective(scenario, True)
            is_min_problem = objfct.is_minimizing()
            nlens = scenario._PySP_nlens
            if (add_prox and 
                'linearize_binary_proximal_terms' in self.PHoptions):
                lin_prox = self.PHoptions['linearize_binary_proximal_terms']
            else:
                lin_prox = False

            for ndn_i, xvar in scenario._nonant_indexes.items():
                ph_term = 0
                # Dual term (weights W)
                if (add_duals):
                    ph_term += \
                        scenario._PHW_on[ndn_i] * scenario._Ws[ndn_i] * xvar
                # Prox term (quadratic)
                if (add_prox):
                    if (xvar.is_binary() and lin_prox):
                        ph_term += scenario._PHprox_on[ndn_i] * \
                            (scenario._PHrho[ndn_i] /2.0) * \
                            (xvar - 2.0 * scenario._xbars[ndn_i] * xvar \
                             - scenario._xbars[ndn_i] \
                             * scenario._xbars[ndn_i])
                    else:
                        ph_term += scenario._PHprox_on[ndn_i] * \
                            (scenario._PHrho[ndn_i] /2.0) * \
                            (xvar - scenario._xbars[ndn_i]) * \
                            (xvar - scenario._xbars[ndn_i])
                if (is_min_problem):
                    objfct.expr += ph_term
                else:
                    objfct.expr -= ph_term

                        
    def attach_Lens_async(self):
            self.Lens = collections.OrderedDict({"OnlyReduce": {}})

            for sname, scenario in self.local_scenarios.items():
                for node in scenario._PySPnode_list:
                    self.Lens["OnlyReduce"][node.name] \
                        = 2 * len(node.nonant_vardata_list)
            self.Lens["OnlyReduce"]["ROOT"] += self.n_proc  # for time of update

    def PH_Prep(
        self, 
        attach_duals=True,
        attach_prox=True,
    ):
        """Create W Params bound by index to nonant-list.
        Also create xsq-bar, which is not strictly needed, but so useful and
        so easy to get along with x-bar, that we get it.

        NOTE: prox_on and W_on are initialized to zero so iteration zero is OK.

        NOTE: xsq is a Pyomo Param even though it doesn't need to be.
        Attach all on each scenario, even though the x-bar ans xsq-bar are
         really per node.
        Also attach _PySP_nlens which is dictionary of nonaticipativity 
        counts indexed by node name.
        NOTE: Called after iteration 0.
        NOTE: returns Nothing, but adds to object:
            comms (dict): key is node name; val is a comm object
        """

        if (self.PH_extensions is not None):
            if self.PH_extension_kwargs is None:
                self.extobject = self.PH_extensions(self)
            else:
                self.extobject = self.PH_extensions(
                    self, **self.PH_extension_kwargs
                )

        self.current_solver_options = self.PHoptions["iter0_solver_options"]

        self.attach_Ws_and_prox()
        self.attach_varid_to_nonant_index()
        self.attach_PH_to_objective(add_duals=attach_duals,
                                    add_prox=attach_prox)

        if ("asynchronousPH" in self.PHoptions
            and self.PHoptions["asynchronousPH"]):
            self.attach_Lens_async()

        if (self.PH_extensions is not None):
            self.extobject.pre_iter0()

    def options_check(self):
        """ Verify that the opions passed in are OK; raise an error if not.
            NOTE: the user can add options of their own, so don't check for
            extras.  
        """
        required = [
            "solvername", "PHIterLimit", "defaultPHrho", 
            "convthresh", "verbose", "display_progress", 
            "display_timing", "iter0_solver_options", "iterk_solver_options"
        ]
        if "asynchronousPH" in self.PHoptions and self.PHoptions["asynchronousPH"]:
            required += ["async_frac_needed", "async_sleep_secs"]
        self._options_check(required, self.PHoptions)

    def subproblem_creation(self, verbose):
        self.local_subproblems = dict()
        if self.bundling:
            rank_local = self.rank
            for bun in self.names_in_bundles[rank_local]:
                sdict = dict()
                bname = "rank" + str(self.rank) + "bundle" + str(bun)
                for sname in self.names_in_bundles[rank_local][bun]:
                    if (verbose and self.rank==self.rank0):
                        print ("bundling "+sname+" into "+bname)
                    sdict[sname] = self.local_scenarios[sname]
                self.local_subproblems[bname] = self.FormEF(sdict, bname)
                self.local_subproblems[bname].scen_list = \
                    self.names_in_bundles[rank_local][bun]
                self.local_subproblems[bname].PySP_prob = \
                                    sum(s.PySP_prob for s in sdict.values())
        else:
            for sname, s in self.local_scenarios.items():
                self.local_subproblems[sname] = s
                self.local_subproblems[sname].scen_list = [sname]

    def _create_solvers(self):
        for sname, s in self.local_subproblems.items(): # solver creation
            s._solver_plugin = SolverFactory(self.PHoptions["solvername"])
            if (sputils.is_persistent(s._solver_plugin)):
                if (self.PHoptions["display_timing"]):
                    set_instance_start_time = time.time()

                s._solver_plugin.set_instance(s) #### JPW: check ph.py for options such as symbolic_solver_labels and output_fixed_variable_bounds

                if (self.PHoptions["display_timing"]):
                    set_instance_time = time.time() - set_instance_start_time
                    all_set_instance_times = self.mpicomm.gather(set_instance_time,
                                                             root=0)
                    if self.rank == self.rank0:
                        print("Set instance times:")
                        print("\tmin=%4.2f mean=%4.2f max=%4.2f" %
                              (np.min(all_set_instance_times),
                               np.mean(all_set_instance_times),
                               np.max(all_set_instance_times)))

    def Iter0(self):
        """ local to main, really; however, some code needs to be called
        directly by the Synchronizer, so main is in three parts (March 2019)
        This is the first part.

        Returns:
            trivial_bound (float): iter 0 trivial bound
        """
        
        verbose = self.PHoptions["verbose"]
        dprogress = self.PHoptions["display_progress"]
        dtiming = self.PHoptions["display_timing"]
        have_extensions = self.PH_extensions is not None
        have_converger = self.PH_converger is not None

        def _vb(msg):
            if verbose and self.rank == self.rank0:
                print("(rank0)", msg)

        self._PHIter = 0
        self._save_original_nonants()

        if self.rank_global == 0:
            tt_timer.toc("Creating solvers", delta=False)
        self._create_solvers()
        
        teeme = False
        if ("tee-rank0-solves" in self.PHoptions):
            teeme = self.PHoptions['tee-rank0-solves']
            
        if self.PHoptions["verbose"]:
            print ("About to call PH Iter0 solve loop on rank={}".format(self.rank))
        if self.rank_global == 0:
            tt_timer.toc("Entering solve loop in PHBase.Iter0", delta=False)

        self.solve_loop(solver_options=self.current_solver_options,
                        dtiming=dtiming,
                        gripe=True,
                        tee=teeme,
                        verbose=verbose)
        
        if self.PHoptions["verbose"]:
            print ("PH Iter0 solve loop complete on rank={}".format(self.rank))
        
        self._update_E1()  # Apologies for doing this after the solves...
        if (abs(1 - self.E1) > self.E1_tolerance):
            if self.rank == self.rank0:
                print("ERROR")
                print("Total probability of scenarios was ", self.E1)
                print("E1_tolerance = ", self.E1_tolerance)
            quit()
        feasP = self.feas_prob()
        if feasP != self.E1:
            if self.rank == self.rank0:
                print("ERROR")
                print("Infeasibility detected; E_feas, E1=", feasP, self.E1)
            quit()

        """
        with open('mpi.out-{}'.format(rank), 'w') as fd:
            for sname in self.local_scenario_names:
                fd.write('*** {} ***\n'.format(sname))
        """
        #timer.toc('Rank: {} - Building and solving models 0th iteration'.format(rank))

        #timer.toc('Rank: {} - assigning rho'.format(rank))

        if have_extensions:
            self.extobject.post_iter0()

        if self.rho_setter is not None:
            if self.rank == self.rank0:
                self._use_rho_setter(verbose)
            else:
                self._use_rho_setter(False)

        converged = False
        if have_converger:
            # Call the constructor of the converger object
            self.convobject = self.PH_converger(self, self.rank, self.n_proc)
        #timer.toc('Rank: {} - Before iter loop'.format(self.rank))
        self.conv = None

        self.trivial_bound = self.Ebound(verbose)

        if dprogress and self.rank == self.rank0:
            print("")
            print("After PH Iteration",self._PHIter)
            print("Trivial bound =", self.trivial_bound)
            print("PHBase Convergence Metric =",self.conv)
            print("Elapsed time: %6.2f" % (dt.datetime.now() - self.startdt).total_seconds())

        self._reenable_W_and_prox()

        self.current_solver_options = self.PHoptions["iterk_solver_options"]

        return self.trivial_bound

    def iterk_loop(self, synchronizer=None):
        """ local to main, really; however, it needs to be called
        directly by the Synchronizer, so main is in three parts (March 2019)

        Args:
            synchronizer (object): deprecated; for asynchronous operation

        Updates: 
            self.conv (): whatever the converger has at termination

        """
        if synchronizer is not None:
            raise RuntimeError("asynchronous PH is not supported (use APH).")
        verbose = self.PHoptions["verbose"]
        have_extensions = self.PH_extensions is not None
        have_converger = self.PH_converger is not None
        dprogress = self.PHoptions["display_progress"]
        dtiming = self.PHoptions["display_timing"]
        self.conv = None

        for self._PHIter in range(1, int(self.PHoptions["PHIterLimit"])+1):
            iteration_start_time = time.time()

            if dprogress and self.rank == self.rank0:
                print("")
                print ("Initiating PH Iteration",self._PHIter)
                print("")

            # Compute xbar
            #timer.toc('Rank: {} - Before Compute_Xbar'.format(self.rank))
            self.Compute_Xbar(verbose, synchronizer)
            #timer.toc('Rank: {} - After Compute_Xbar'.format(self.rank))

            # update the weights        
            self.Update_W(verbose)
            #timer.toc('Rank: {} - After Update_W'.format(self.rank))

            if have_converger:
                self.conv = self.convobject.convergence_value()
            elif synchronizer is None:
                self.conv = self.convergence_diff()
            #timer.toc('Rank: {} - After convergence_diff'.format(self.rank))
            if have_extensions:
                self.extobject.miditer()

            # The hub object takes precedence 
            # over the converger
            if self.spcomm is not None:
                self.spcomm.sync()
                if self.spcomm.is_converged():
                    if self.rank == self.rank0:
                        tt_timer.toc("Cylinder convergence", delta=False)
                    break    
            if have_converger:
                if self.convobject.is_converged():
                    converged = True
                    if self.rank == self.rank0:
                        tt_timer.toc("User-supplied converger determined termination criterion reached", delta=False)
                    break
            elif synchronizer is None and self.conv is not None:
                if self.conv < self.PHoptions["convthresh"]:
                    converged = True
                    if self.rank == self.rank0:
                        tt_timer.toc("PHBase Convergence metric=%f dropped below user-supplied threshold=%f" % (self.conv, self.PHoptions["convthresh"]), delta=False)
                    break

            teeme = (
                "tee-rank0-solves" in self.PHoptions
                 and self.PHoptions["tee-rank0-solves"]
            )
            self.solve_loop(
                solver_options=self.current_solver_options,
                dtiming=dtiming,
                gripe=True,
                disable_pyomo_signal_handling=synchronizer is not None,
                tee=teeme,
                verbose=verbose
            )

            if have_extensions:
                self.extobject.enditer()

            if dprogress and self.rank == self.rank0:
                print("")
                print("After PH Iteration",self._PHIter)
                print("Scaled PHBase Convergence Metric=",self.conv)
                print("Iteration time: %6.2f" % (time.time() - iteration_start_time))
                print("Elapsed time:   %6.2f" % (dt.datetime.now() - self.startdt).total_seconds())
        if synchronizer is not None:
            logger.debug('Setting synchronizer.quitting on rank %d' % self.rank)
            synchronizer.quitting = 1

    def post_loops(self,
                   PH_extensions = None):
        """ local to main, really; however, some code needs to be called
        directly by the Synchronizer, so main is in three parts (March 2019)
        This is the last part.
        Args:
            conv (): whatever the converger gave
            PH_extensions (object) : optional
        Returns:
            Eobj (float): pretty useless weighted, proxed obj value

        """
        verbose = self.PHoptions["verbose"]
        have_extensions = PH_extensions is not None
        dprogress = self.PHoptions["display_progress"]
        dtiming = self.PHoptions["display_timing"]

        # for reporting sanity
        self.mpicomm.Barrier()

        if self.rank == self.rank0 and dprogress:
            print("")
            print("Invoking scenario reporting functions, if applicable")
            print("")

        if self.scenario_denouement is not None:
            for sname,s in self.local_scenarios.items():
                self.scenario_denouement(self.rank, sname, s)

        self.mpicomm.Barrier()

        if self.rank == self.rank0 and dprogress:
            print("")
            print("Invoking PH extension finalization, if applicable")    
            print("")

        if have_extensions:
            self.extobject.post_everything()

        Eobj = self.Eobjective(verbose)

        self.mpicomm.Barrier()

        if dprogress and self.rank == self.rank0:
            print("")
            print("Current ***weighted*** E[objective] =", Eobj)
            print("")

        if dtiming and self.rank == self.rank0:
            print("")
            print("Cumulative execution time=%5.2f" % (time.time()-self.start_time))
            print("")

        return Eobj

    def attach_xbars(self):
        for scenario in self.local_scenarios.values():
            scenario._xbars = pyo.Param(
                scenario._nonant_indexes.keys(), initialize=0.0, mutable=True
            )
            scenario._xsqbars = pyo.Param(
                scenario._nonant_indexes.keys(), initialize=0.0, mutable=True
            )

    def gather_var_values_to_root(self):
        ''' Gather only the nonants to the root of mpicomm
            Gathers only the scalar variable values, not the Pyomo
            Var objects.
        '''
        var_values = dict()
        for (sname, model) in self.local_scenarios.items():
            for node in model._PySPnode_list:
                for var in node.nonant_vardata_list:
                    var_values[sname, var.name] = pyo.value(var)

        result = self.mpicomm.gather(var_values, root=self.rank0)

        if (self.rank == self.rank0):
            result = {key: value
                for dic in result
                for (key, value) in dic.items()
            }
            return result

if __name__ == "__main__":
    print ("No main for PHBase")
