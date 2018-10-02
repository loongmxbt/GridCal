# This file is part of GridCal.
#
# GridCal is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GridCal is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GridCal.  If not, see <http://www.gnu.org/licenses/>.

from warnings import warn
import pulp
import numpy as np
from scipy.sparse import csc_matrix

from GridCal.Engine.BasicStructures import BusMode
from GridCal.Engine.PowerFlowDriver import PowerFlowMP
from GridCal.Engine.IoStructures import CalculationInputs, OptimalPowerFlowResults
from GridCal.Engine.CalculationEngine import MultiCircuit


class DcOpf_old:

    def __init__(self, calculation_input: CalculationInputs=None, buses=list(), branches=list(), options=None):
        """
        OPF simple dispatch problem
        :param calculation_input: GridCal Circuit instance (remember this must be a connected island)
        :param options: OptimalPowerFlowOptions instance
        """

        self.calculation_input = calculation_input

        self.buses = buses
        self.buses_dict = {bus: i for i, bus in enumerate(buses)}  # dictionary of bus objects given their indices
        self.branches = branches

        self.options = options

        if options is not None:
            self.load_shedding = options.load_shedding
        else:
            self.load_shedding = False

        self.Sbase = calculation_input.Sbase
        self.B = calculation_input.Ybus.imag.tocsr()
        self.nbus = calculation_input.nbus
        self.nbranch = calculation_input.nbr

        # node sets
        self.pqpv = calculation_input.pqpv
        self.pv = calculation_input.pv
        self.vd = calculation_input.ref
        self.pq = calculation_input.pq

        # declare the voltage angles and the possible load shed values
        self.theta = np.empty(self.nbus, dtype=object)
        self.load_shed = np.empty(self.nbus, dtype=object)
        for i in range(self.nbus):
            self.theta[i] = pulp.LpVariable("Theta_" + str(i), -0.5, 0.5)
            self.load_shed[i] = pulp.LpVariable("LoadShed_" + str(i), 0.0, 1e20)

        # declare the slack vars
        self.slack_loading_ij_p = np.empty(self.nbranch, dtype=object)
        self.slack_loading_ji_p = np.empty(self.nbranch, dtype=object)
        self.slack_loading_ij_n = np.empty(self.nbranch, dtype=object)
        self.slack_loading_ji_n = np.empty(self.nbranch, dtype=object)

        if self.load_shedding:
            pass

        else:

            for i in range(self.nbranch):
                self.slack_loading_ij_p[i] = pulp.LpVariable("LoadingSlack_ij_p_" + str(i), 0, 1e20)
                self.slack_loading_ji_p[i] = pulp.LpVariable("LoadingSlack_ji_p_" + str(i), 0, 1e20)
                self.slack_loading_ij_n[i] = pulp.LpVariable("LoadingSlack_ij_n_" + str(i), 0, 1e20)
                self.slack_loading_ji_n[i] = pulp.LpVariable("LoadingSlack_ji_n_" + str(i), 0, 1e20)

        # declare the generation
        self.PG = list()

        # LP problem
        self.problem = None

        # potential errors flag
        self.potential_errors = False

        # Check if the problem was solved or not
        self.solved = False

        # LP problem restrictions saved on build and added to the problem with every load change
        self.calculated_node_power = list()
        self.node_power_injections = list()

        self.node_total_load = np.zeros(self.nbus)

    def copy(self):

        obj = DcOpf(calculation_input=self.calculation_input, options=self.options)

        obj.calculation_input = self.calculation_input

        obj.buses = self.buses
        obj.buses_dict = self.buses_dict
        obj.branches = self.branches

        obj.load_shedding = self.load_shedding

        obj.Sbase = self.Sbase
        obj.B = self.B
        obj.nbus = self.nbus
        obj.nbranch = self.nbranch

        # node sets
        obj.pqpv = self.pqpv.copy()
        obj.pv = self.pv.copy()
        obj.vd = self.vd.copy()
        obj.pq = self.pq.copy()

        # declare the voltage angles and the possible load shed values
        obj.theta = self.theta.copy()
        obj.load_shed = self.load_shed.copy()

        # declare the slack vars
        obj.slack_loading_ij_p = self.slack_loading_ij_p.copy()
        obj.slack_loading_ji_p = self.slack_loading_ji_p.copy()
        obj.slack_loading_ij_n = self.slack_loading_ij_n.copy()
        obj.slack_loading_ji_n = self.slack_loading_ji_n.copy()

        # declare the generation
        obj.PG = self.PG.copy()

        # LP problem
        obj.problem = self.problem.copy()

        # potential errors flag
        obj.potential_errors = self.potential_errors

        # Check if the problem was solved or not
        obj.solved = self.solved

        # LP problem restrictions saved on build and added to the problem with every load change
        obj.node_power_injections = self.node_power_injections.copy()
        obj.calculated_node_power = self.calculated_node_power.copy()

        obj.node_total_load = np.zeros(self.nbus)

        return obj

    def build(self, t_idx=None):
        """
        Build the OPF problem using the sparse formulation
        In this step, the circuit loads are not included
        those are added separately for greater flexibility
        """

        '''
        CSR format explanation:
        The standard CSR representation where the column indices for row i are stored in 

        -> indices[indptr[i]:indptr[i+1]] 

        and their corresponding values are stored in 

        -> data[indptr[i]:indptr[i+1]]

        If the shape parameter is not supplied, the matrix dimensions are inferred from the index arrays.

        https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.csr_matrix.html
        '''

        print('Compiling LP')
        prob = pulp.LpProblem("DC optimal power flow", pulp.LpMinimize)

        # initialize the potential errors
        self.potential_errors = False

        ################################################################################################################
        # Add the objective function
        ################################################################################################################
        fobj = 0.0

        # add the voltage angles multiplied by zero (trick)
        # for j in self.pqpv:
        #     fobj += self.theta[j] * 0.0
        fobj += (self.theta[self.pqpv] * 0).sum()

        # Add the generators cost
        for k, bus in enumerate(self.buses):

            generators = bus.controlled_generators + bus.batteries

            # check that there are at least one generator at the slack node
            if len(generators) == 0 and bus.type == BusMode.REF:
                self.potential_errors = True
                warn('There is no generator at the Slack node ' + bus.name + '!!!')

            # Add the bus LP vars
            for i, gen in enumerate(generators):

                # add the variable to the objective function
                if gen.active and gen.enabled_dispatch:
                    if t_idx is None:
                        fobj += gen.LPVar_P * gen.Cost
                        # add the var reference just to print later...
                        self.PG.append(gen.LPVar_P)
                    else:
                        fobj += gen.LPVar_P_prof[t_idx] * gen.Cost
                        # add the var reference just to print later...
                        self.PG.append(gen.LPVar_P_prof[t_idx])
                else:
                    pass  # the generator is not active

            # minimize the load shedding if activated
            if self.load_shedding:
                fobj += self.load_shed[k]

        # minimize the branch loading slack if not load shedding
        if not self.load_shedding:
            # Minimize the branch overload slacks
            for k, branch in enumerate(self.branches):
                if branch.active:
                    fobj += self.slack_loading_ij_p[k] + self.slack_loading_ij_n[k]
                    fobj += self.slack_loading_ji_p[k] + self.slack_loading_ji_n[k]
                else:
                    pass  # the branch is not active

        # Add the objective function to the problem
        prob += fobj

        ################################################################################################################
        # Add the nodal power balance equations as constraints (without loads, those are added later)
        # See: https://math.stackexchange.com/questions/1727572/solving-a-feasible-system-of-linear-equations-
        #      using-linear-programming
        ################################################################################################################
        for i in self.pqpv:

            calculated_node_power = 0
            node_power_injection = 0
            generators = self.buses[i].controlled_generators + self.buses[i].batteries

            # add the calculated node power
            for ii in range(self.B.indptr[i], self.B.indptr[i + 1]):

                j = self.B.indices[ii]

                if j not in self.vd:

                    calculated_node_power += self.B.data[ii] * self.theta[j]

                    if self.B.data[ii] < 1e-6:
                        warn("There are susceptances close to zero.")

            # add the generation LP vars
            if t_idx is None:
                for gen in generators:
                    if gen.active:
                        if gen.enabled_dispatch:
                            # add the dispatch variable
                            node_power_injection += gen.LPVar_P
                        else:
                            # set the default value
                            node_power_injection += gen.P / self.Sbase
                    else:
                        pass
            else:
                for gen in generators:
                    if gen.active:
                        if gen.enabled_dispatch:
                            # add the dispatch variable
                            node_power_injection += gen.LPVar_P_prof[t_idx]
                        else:
                            # set the default profile value
                            node_power_injection += gen.Pprof.values[t_idx] / self.Sbase
                    else:
                        pass

            # Store the terms for adding the load later.
            # This allows faster problem compilation in case of recurrent runs
            self.calculated_node_power.append(calculated_node_power)
            self.node_power_injections.append(node_power_injection)

            # # add the nodal demand: See 'set_loads()'
            # for load in self.circuit.buses[i].loads:
            #     node_power_injection -= load.S.real / self.Sbase
            #
            # prob.add(calculated_node_power == node_power_injection, 'ct_node_mismatch_' + str(i))

        ################################################################################################################
        #  set the slack nodes voltage angle
        ################################################################################################################
        for i in self.vd:
            prob.add(self.theta[i] == 0, 'ct_slack_theta_' + str(i))

        ################################################################################################################
        #  set the slack generator power
        ################################################################################################################
        for i in self.vd:

            val = 0
            g = 0
            generators = self.buses[i].controlled_generators + self.buses[i].batteries

            # compute the slack node power
            for ii in range(self.B.indptr[i], self.B.indptr[i + 1]):
                j = self.B.indices[ii]
                val += self.B.data[ii] * self.theta[j]

            # Sum the slack generators
            if t_idx is None:
                for gen in generators:
                    if gen.active and gen.enabled_dispatch:
                        g += gen.LPVar_P
                    else:
                        pass
            else:
                for gen in generators:
                    if gen.active and gen.enabled_dispatch:
                        g += gen.LPVar_P_prof[t_idx]
                    else:
                        pass

            # the sum of the slack node generators must be equal to the slack node power
            prob.add(g == val, 'ct_slack_power_' + str(i))

        ################################################################################################################
        # Set the branch limits
        ################################################################################################################
        any_rate_zero = False
        for k, branch in enumerate(self.branches):

            if branch.active:
                i = self.buses_dict[branch.bus_from]
                j = self.buses_dict[branch.bus_to]

                # branch flow
                Fij = self.B[i, j] * (self.theta[i] - self.theta[j])
                Fji = self.B[i, j] * (self.theta[j] - self.theta[i])

                # constraints
                if not self.load_shedding:
                    # Add slacks
                    prob.add(Fij + self.slack_loading_ij_p[k] - self.slack_loading_ij_n[k] <= branch.rate / self.Sbase,
                             'ct_br_flow_ij_' + str(k))
                    prob.add(Fji + self.slack_loading_ji_p[k] - self.slack_loading_ji_n[k] <= branch.rate / self.Sbase,
                             'ct_br_flow_ji_' + str(k))
                    # prob.add(Fij <= branch.rate / self.Sbase, 'ct_br_flow_ij_' + str(k))
                    # prob.add(Fji <= branch.rate / self.Sbase, 'ct_br_flow_ji_' + str(k))
                else:
                    # The slacks are in the form of load shedding
                    prob.add(Fij <= branch.rate / self.Sbase, 'ct_br_flow_ij_' + str(k))
                    prob.add(Fji <= branch.rate / self.Sbase, 'ct_br_flow_ji_' + str(k))

                if branch.rate <= 1e-6:
                    any_rate_zero = True
            else:
                pass  # the branch is not active...

        # No branch can have rate = 0, otherwise the problem fails
        if any_rate_zero:
            self.potential_errors = True
            warn('There are branches with no rate.')

        # set the global OPF LP problem
        self.problem = prob

    def set_loads(self, t_idx=None):
        """
        Add the loads to the LP problem
        Args:
            t_idx: time index, if none, the default object values are taken
        """

        self.node_total_load = np.zeros(self.nbus)

        if t_idx is None:

            # use the default loads
            for k, i in enumerate(self.pqpv):

                # these restrictions come from the build step to be fulfilled with the load now
                node_power_injection = self.node_power_injections[k]
                calculated_node_power = self.calculated_node_power[k]

                # add the nodal demand
                for load in self.buses[i].loads:
                    if load.active:
                        self.node_total_load[i] += load.S.real / self.Sbase
                    else:
                        pass

                # Add non dispatcheable generation
                generators = self.buses[i].controlled_generators + self.buses[i].batteries
                for gen in generators:
                    if gen.active and not gen.enabled_dispatch:
                        self.node_total_load[i] -= gen.P / self.Sbase

                # Add the static generators
                for gen in self.buses[i].static_generators:
                    if gen.active:
                        self.node_total_load[i] -= gen.S.real / self.Sbase

                if calculated_node_power is 0 and node_power_injection is 0:
                    # nodes without injection or generation
                    pass
                else:
                    # add the restriction
                    if self.load_shedding:

                        self.problem.add(
                            calculated_node_power == node_power_injection - self.node_total_load[i] + self.load_shed[i],
                            self.buses[i].name + '_ct_node_mismatch_' + str(k))

                        # if there is no load at the node, do not allow load shedding
                        if len(self.buses[i].loads) == 0:
                            self.problem.add(self.load_shed[i] == 0.0,
                                             self.buses[i].name + '_ct_null_load_shed_' + str(k))

                    else:
                        self.problem.add(calculated_node_power == node_power_injection - self.node_total_load[i],
                                         self.buses[i].name + '_ct_node_mismatch_' + str(k))
        else:
            # Use the load profile values at index=t_idx
            for k, i in enumerate(self.pqpv):

                # these restrictions come from the build step to be fulfilled with the load now
                node_power_injection = self.node_power_injections[k]
                calculated_node_power = self.calculated_node_power[k]

                # add the nodal demand
                for load in self.buses[i].loads:
                    if load.active:
                        self.node_total_load[i] += load.Sprof.values[t_idx].real / self.Sbase
                    else:
                        pass

                # Add non dispatcheable generation
                generators = self.buses[i].controlled_generators + self.buses[i].batteries
                for gen in generators:
                    if gen.active and not gen.enabled_dispatch:
                        self.node_total_load[i] -= gen.Pprof.values[t_idx] / self.Sbase

                # Add the static generators
                for gen in self.buses[i].static_generators:
                    if gen.active:
                        self.node_total_load[i] -= gen.Sprof.values[t_idx].real / self.Sbase

                # add the restriction
                if self.load_shedding:

                    self.problem.add(
                        calculated_node_power == node_power_injection - self.node_total_load[i] + self.load_shed[i],
                        self.buses[i].name + '_ct_node_mismatch_' + str(k))

                    # if there is no load at the node, do not allow load shedding
                    if len(self.buses[i].loads) == 0:
                        self.problem.add(self.load_shed[i] == 0.0,
                                         self.buses[i].name + '_ct_null_load_shed_' + str(k))

                else:
                    self.problem.add(calculated_node_power == node_power_injection - self.node_total_load[i],
                                     self.buses[i].name + '_ct_node_mismatch_' + str(k))

    def solve(self):
        """
        Solve the LP OPF problem
        """

        if not self.potential_errors:

            # if there is no problem there, make it
            if self.problem is None:
                self.build()

            print('Solving LP')
            print('Load shedding:', self.load_shedding)
            self.problem.solve()  # solve with CBC
            # prob.solve(CPLEX())

            # self.problem.writeLP('dcopf.lp')

            # The status of the solution is printed to the screen
            print("Status:", pulp.LpStatus[self.problem.status])

            # The optimised objective function value is printed to the screen
            print("Cost =", pulp.value(self.problem.objective), '€')

            self.solved = True

        else:
            self.solved = False

    def print(self):
        """
        Print results
        :return:
        """
        print('\nVoltage angles (in rad)')
        for i, th in enumerate(self.theta):
            print('Bus', i, '->', th.value())

        print('\nGeneration power (in MW)')
        for i, g in enumerate(self.PG):
            val = g.value() * self.Sbase if g.value() is not None else 'None'
            print(g.name, '->', val)

        # Set the branch limits
        print('\nBranch flows (in MW)')
        for k, branch in enumerate(self.branches):
            i = self.buses_dict[branch.bus_from]
            j = self.buses_dict[branch.bus_to]
            if self.theta[i].value() is not None and self.theta[j].value() is not None:
                F = self.B[i, j] * (self.theta[i].value() - self.theta[j].value()) * self.Sbase
            else:
                F = 'None'
            print('Branch ' + str(i) + '-' + str(j) + '(', branch.rate, 'MW) ->', F)

    def get_results(self, save_lp_file=False, t_idx=None, realistic=False):
        """
        Return the optimization results
        :param save_lp_file:
        :param t_idx:
        :param realistic:
        :return: OptimalPowerFlowResults instance
        """

        # initialize results object
        n = len(self.buses)
        m = len(self.branches)
        res = OptimalPowerFlowResults(is_dc=True)
        res.initialize(n, m)

        if save_lp_file:
            # export the problem formulation to an LP file
            self.problem.writeLP('dcopf.lp')

        if self.solved:

            if realistic:

                # Add buses
                for i in range(n):
                    # Set the voltage
                    res.voltage[i] = 1 * np.exp(1j * self.theta[i].value())

                    if self.load_shed is not None:
                        res.load_shedding[i] = self.load_shed[i].value()

                # Set the values
                res.Sbranch, res.Ibranch, res.loading, \
                res.losses, res.flow_direction, res.Sbus = PowerFlowMP.power_flow_post_process(self.calculation_input, res.voltage)

            else:
                # Add buses
                for i in range(n):
                    # g = 0.0
                    #
                    generators = self.buses[i].controlled_generators + self.buses[i].batteries

                    # copy the generators LpVAr
                    if t_idx is None:
                        pass
                        # for gen in generators:
                        #     if gen.active and gen.enabled_dispatch:
                        #         g += gen.LPVar_P.value()
                    else:
                        # copy the LpVar
                        for gen in generators:
                            if gen.active and gen.enabled_dispatch:
                                val = gen.LPVar_P.value()
                                var = pulp.LpVariable('')
                                var.varValue = val
                                gen.LPVar_P_prof[t_idx] = var
                    #
                    # # Set the results
                    # res.Sbus[i] = (g - self.loads[i]) * self.circuit.Sbase

                    # Set the voltage
                    res.voltage[i] = 1 * np.exp(1j * self.theta[i].value())

                    if self.load_shed is not None:
                        res.load_shedding[i] = self.load_shed[i].value()

                # Set the values
                res.Sbranch, res.Ibranch, res.loading, \
                res.losses, res.flow_direction, res.Sbus = PowerFlowMP.power_flow_post_process(self.calculation_input, res.voltage, only_power=True)

                # Add branches
                for k, branch in enumerate(self.branches):

                    if branch.active:
                        # get the from and to nodal indices of the branch
                        i = self.buses_dict[branch.bus_from]
                        j = self.buses_dict[branch.bus_to]

                        # compute the power flowing
                        if self.theta[i].value() is not None and self.theta[j].value() is not None:
                            F = self.B[i, j] * (self.theta[i].value() - self.theta[j].value()) * self.Sbase
                        else:
                            F = -1

                        # Set the results
                        if self.slack_loading_ij_p[k] is not None:
                            res.overloads[k] = (self.slack_loading_ij_p[k].value()
                                                + self.slack_loading_ji_p[k].value()
                                                - self.slack_loading_ij_n[k].value()
                                                - self.slack_loading_ji_n[k].value()) * self.Sbase
                        res.Sbranch[k] = F
                        res.loading[k] = abs(F / branch.rate)
                    else:
                        pass

        else:
            # the problem did not solve, pass
            pass

        return res


def Cproduct(C, vect):
    """
    Connectivity matrix-vector product
    :param C: Connectivity matrix
    :param vect: vector of object type
    :return:
    """
    n_rows, n_cols = C.shape
    res = np.zeros(n_cols, dtype=object)
    for i in range(n_cols):
        # compute the slack node power
        for ii in range(C.indptr[i], C.indptr[i + 1]):
            j = C.indices[ii]
            res[i] += C.data[ii] * vect[j]
    return res


class DcOpf:

    def __init__(self, multi_circuit: MultiCircuit, verbose=False,
                 allow_load_shedding=False, allow_generation_shedding=False):
        """

        :param multi_circuit:
        :param verbose:
        :param allow_load_shedding:
        :param allow_generation_shedding:
        """

        # flags
        self.verbose = verbose
        self.allow_load_shedding = allow_load_shedding
        self.allow_generation_shedding = allow_generation_shedding

        # circuit compilation
        self.multi_circuit = multi_circuit
        self.numerical_circuit = self.multi_circuit.compile()
        self.islands = self.numerical_circuit.compute()

        # get the devices
        controlled_generators = self.multi_circuit.get_controlled_generators()
        batteries = self.multi_circuit.get_batteries()
        loads = self.multi_circuit.get_loads()

        # shortcuts...
        nbus = self.numerical_circuit.nbus
        nbr = self.numerical_circuit.nbr
        ngen = len(controlled_generators)
        nbat = len(batteries)
        Sbase = self.multi_circuit.Sbase

        # bus angles
        self.theta = np.array([pulp.LpVariable("Theta_" + str(i), -0.5, 0.5) for i in range(nbus)])

        # Generator variables (P and P shedding)
        self.controlled_generators_P = np.empty(ngen, dtype=object)
        self.controlled_generators_cost = np.zeros(ngen)
        self.generation_shedding = np.empty(ngen, dtype=object)

        for i, gen in enumerate(controlled_generators):
            name = 'GEN_' + gen.name + '_' + str(i)
            pmin = gen.Pmin / Sbase
            pmax = gen.Pmax / Sbase
            self.controlled_generators_P[i] = pulp.LpVariable(name + '_P',  pmin, pmax)
            self.generation_shedding[i] = pulp.LpVariable(name + '_SHEDDING', 0.0, 1e20)
            self.controlled_generators_cost[i] = gen.Cost

        # Batteries
        self.battery_P = np.empty(nbat, dtype=object)
        self.battery_cost = np.zeros(nbat)
        for i, battery in enumerate(batteries):
            name = 'BAT_' + battery.name + '_' + str(i)
            pmin = battery.Pmin / Sbase
            pmax = battery.Pmax / Sbase
            self.battery_P[i] = pulp.LpVariable(name + '_P', pmin, pmax)
            self.battery_cost[i] = battery.Cost

        # load shedding
        self.load_shedding = np.array([pulp.LpVariable("LoadShed_" + load.name + '_' + str(i), 0.0, 1e20)
                                       for i, load in enumerate(loads)])

        # declare the loading slack vars
        self.slack_loading_ij_p = np.empty(nbr, dtype=object)
        self.slack_loading_ji_p = np.empty(nbr, dtype=object)
        self.slack_loading_ij_n = np.empty(nbr, dtype=object)
        self.slack_loading_ji_n = np.empty(nbr, dtype=object)
        for i in range(nbr):
            self.slack_loading_ij_p[i] = pulp.LpVariable("LoadingSlack_ij_p_" + str(i), 0, 1e20)
            self.slack_loading_ji_p[i] = pulp.LpVariable("LoadingSlack_ji_p_" + str(i), 0, 1e20)
            self.slack_loading_ij_n[i] = pulp.LpVariable("LoadingSlack_ij_n_" + str(i), 0, 1e20)
            self.slack_loading_ji_n[i] = pulp.LpVariable("LoadingSlack_ji_n_" + str(i), 0, 1e20)

    def build_solvers(self):
        """
        Builds the solvers for each island
        :return:
        """

        # Sbase shortcut
        Sbase = self.numerical_circuit.Sbase

        # objective contributions of generators
        fobj_gen = Cproduct(csc_matrix(self.numerical_circuit.C_ctrl_gen_bus), self.controlled_generators_P * self.controlled_generators_cost)

        # objective contribution of the batteries
        fobj_bat = Cproduct(csc_matrix(self.numerical_circuit.C_batt_bus), self.battery_P * self.battery_cost)

        # LP variables for the controlled generators
        P = Cproduct(csc_matrix(self.numerical_circuit.C_ctrl_gen_bus), self.controlled_generators_P)

        # LP variables for the batteries
        P += Cproduct(csc_matrix(self.numerical_circuit.C_batt_bus), self.battery_P)

        # Loads for all the circuits
        P -= self.numerical_circuit.C_load_bus.T * (self.numerical_circuit.load_power.real / Sbase *
                                                    self.numerical_circuit.load_enabled)

        # static generators for all the circuits
        P += self.numerical_circuit.C_sta_gen_bus.T * (self.numerical_circuit.static_gen_power.real / Sbase *
                                                       self.numerical_circuit.static_gen_enabled)

        # controlled generators for all the circuits (enabled and not dispatchable)
        P += self.numerical_circuit.C_ctrl_gen_bus.T * (self.numerical_circuit.controlled_gen_power / Sbase *
                                                        self.numerical_circuit.controlled_gen_enabled *
                                                        np.logical_not(self.numerical_circuit.controlled_gen_dispatchable))

        theta_f = Cproduct(csc_matrix(self.numerical_circuit.C_branch_bus_f.T), self.theta)
        theta_t = Cproduct(csc_matrix(self.numerical_circuit.C_branch_bus_t.T), self.theta)

        Btotal = self.numerical_circuit.get_B()
        B_br = np.ravel(Btotal[self.numerical_circuit.F, self.numerical_circuit.T]).T

        for island in self.islands:

            prob = pulp.LpProblem("DC optimal power flow", pulp.LpMinimize)

            b_idx = island.original_bus_idx
            br_idx = island.original_branch_idx

            # Objective function
            fobj = fobj_gen[b_idx].sum() + fobj_bat[b_idx].sum()
            prob += fobj

            # susceptance matrix
            B = island.Ybus.imag

            # declare the array of calculated power at all the nodes
            calculated_power = np.zeros(island.nbus, dtype=object)

            # calculated power at the non-slack nodes
            calculated_power[island.pqpv] = Cproduct(B[island.pqpv, :][:, island.pqpv].T, self.theta[island.pqpv])

            # calculated power at the slack nodes
            calculated_power[island.ref] = Cproduct(B[island.ref, :][:, island.pqpv].T, self.theta[island.pqpv])

            # rating restrictions -> Bij * (theta_i - theta_j), for the island branches
            rating_restriction_ft = B_br[br_idx] * (theta_f[br_idx] - theta_t[br_idx])
            rating_restriction_tf = B_br[br_idx] * (theta_t[br_idx] - theta_f[br_idx])

            # modify the restrictions is there is load shedding
            if self.allow_load_shedding:
                rating_restriction_ft += self.slack_loading_ij_p[br_idx] - self.slack_loading_ij_n[br_idx]
                rating_restriction_tf += self.slack_loading_ji_p[br_idx] - self.slack_loading_ji_n[br_idx]

            # add the rating restrictions to the problem
            for i in range(island.nbr):

                prob.addConstraint(rating_restriction_ft[i] <= island.branch_rates[i] / Sbase)
                prob.addConstraint(rating_restriction_tf[i] <= island.branch_rates[i] / Sbase)

            prob.solve()
            print("Status:", pulp.LpStatus[prob.status])

            # The optimised objective function value is printed to the screen
            print("Cost =", pulp.value(prob.objective), '€')
            pass

    def solve(self):
        """
        Solve all islands
        :return:
        """
        pass


if __name__ == '__main__':

    main_circuit = MultiCircuit()
    fname = 'D:\\GitHub\\GridCal\\Grids_and_profiles\\grids\\IEEE 30 Bus with storage.xlsx'
    # fname = '/home/santi/Documentos/GitHub/GridCal/Grids_and_profiles/grids/IEEE 30 Bus with storage.xlsx'

    print('Reading...')
    main_circuit.load_file(fname)

    problem = DcOpf(main_circuit)

    problem.build_solvers()
