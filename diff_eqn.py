import copy
import os
import shutil
import webbrowser
from datetime import datetime
from math import comb

import imageio
import matplotlib.pyplot as plt
import neal
import numpy as np
import scipy.integrate as integrate
from dwave.system import DWaveSampler, EmbeddingComposite, LazyFixedEmbeddingComposite
from scipy.misc import derivative
import math

from basisfunctions import BasisFunctionsArray, calculate_S
from graph import show_bqm_graph
from helper_functions import (
    A_matrix,
    compute_a_min,
    compute_all_J_tildes,
    create_bqm,
    feasible_solution,
)
from progressbar import ProgressBar


def simulated_sample(bqm, **kwargs):
    sim_solver = neal.SimulatedAnnealingSampler()
    sampleset = sim_solver.sample(bqm, num_reads=1000, **kwargs).aggregate()
    return sampleset


def real_sample(bqm, **kwargs):
    real_solver = EmbeddingComposite(DWaveSampler())
    return real_solver.sample(bqm, **kwargs).aggregate()


def lazy_sample(bqm, **kwargs):
    real_solver = LazyFixedEmbeddingComposite(DWaveSampler())
    return real_solver.sample(bqm, num_reads=10000, **kwargs).aggregate()


def sample(bqm, sampler, filter=False, **kwargs):
    if not filter:
        return sampler.sample(bqm, **kwargs).aggregate()
    else:
        while True:
            sampleset = sampler.sample(bqm, **kwargs).filter(feasible_solution)
            if len(sampleset) > 0:
                return sampleset


index_to_q_triplet = {
    1: (1, -1, -1),
    2: (-1, 1, -1),
    3: (-1, -1, 1),
}


class DiffEqn:
    def __init__(
        self, initial_condition, nodes, S, boundary_condition, basis_functions
    ) -> None:
        self.S = S
        self.initial_condition = initial_condition
        self.boundary_condition = boundary_condition
        self.basis_functions_shape = basis_functions
        self.N = len(initial_condition) - 1
        self.nodes = nodes
        if len(self.initial_condition) != len(self.nodes):
            raise ValueError(
                f"Lengths of nodes ({len(nodes)}) and initial condition ({len(self.initial_condition)}) do not match"
            )
        self.solution = None
        self.solution_iterates = []
        self.bqm_iterates = []
        self.a_min_iterates = []
        self.r_iterates = []
        self.i = 0

    def solution_function(self, coefficients=None):
        """Returns the function x |-> \sum a[i]*\phi_i(x) where a[i] are the given coefficients.
        Uses the optimal solution by default, but you can pass the coefficients if you want to plot
        the function corresponding to those.
        coefficients

        Args:
            coefficients (list[float], optional): _description_. Defaults to None.

        Returns:
            function float -> float: the function x |-> \sum a[i]*\phi_i(x)
        """
        basis_functions = BasisFunctionsArray(self.nodes, self.basis_functions_shape)
        if coefficients is None:
            coefficients = self.solution

        @np.vectorize
        def fct(x):
            total = 0
            for coeff, basis_fct in zip(coefficients, basis_functions):
                total += coeff * basis_fct(x)
            return total

        return fct

    def plot_solution(self, coefficients=None, ax=plt, r_range=False, r=None, **kwargs):
        """Plots the solution given by DiffEqn.solution_function

        Args:
            coefficients (list[float], optional): list of coefficients. Defaults to None.
        """
        x_axis = np.linspace(self.nodes[0], self.nodes[-1], 1000)
        sol_fct = self.solution_function(coefficients)
        y = sol_fct(x_axis)
        ax.plot(x_axis, y, **kwargs)
        if r_range:
            if r is None:
                r = self.r_iterates[-1]
            plt.fill_between(x_axis, y - r, y + r, color="#ef3936", alpha=0.5)

    def get_default_plotter(self, r_range=False, graph=False):
        def plot(i):
            u_c = self.solution_iterates[i]
            if graph:
                plt.subplot(211)
            if r_range:
                self.plot_solution(u_c, r_range=r_range, r=self.r_iterates[i])
            else:
                self.plot_solution(u_c)
            if graph:
                plt.subplot(212)
                bqm = self.bqm_iterates[i]
                show_bqm_graph(bqm, show=False)
            plt.title(f"Iteration {i}")

        return plot

    def solve(
        self,
        r,
        r_min=None,
        Pi_min=None,
        sampler=None,
        H=1,
        J_hat=1,
        b_c_strength=1,
        sampler_config=None,
        maximize=False,
        progress_bar=False,
        maxiter=math.inf,
        r_factor=2,
    ):
        if sampler is None:
            sampler = simulated_sample

        if sampler_config is None:
            sampler_config = {}

        if (r_min is None) == (Pi_min is None):
            raise ValueError("Set either r_min or Pi_min for the stopping condition")

        u_c = copy.copy(self.initial_condition)
        self.solution_iterates = []
        self.r_iterates = []
        self.bqm_iterates = []
        self.a_min_iterates = []
        self.r_iterates = []
        if progress_bar:
            pb = ProgressBar(int(np.log(r / r_min) / np.log(r_factor)) + 1, width=30)

        self.i = 0
        while r > r_min and self.i <= maxiter:
            self.r_iterates.append(r)
            J_tildes = self.compute_all_J_tildes(u_c, r)
            bqm = create_bqm(
                H,
                J_hat,
                J_tildes,
                boundary_condition=self.boundary_condition,
                b_c_strength=b_c_strength,
                maximize=maximize,
            )
            self.bqm_iterates.append(bqm)
            sampleset = sampler(bqm, **sampler_config)
            # solver.sample(bqm)  # adjust this to the solver!
            a_min = compute_a_min(sampleset, u_c, r)
            self.a_min_iterates.append(a_min)
            if self.Pi_functional(a_min) < self.Pi_functional(u_c):
                u_c = a_min
            else:
                if progress_bar:
                    pb.tick(extra=f"(i={self.i})")
                r /= r_factor

            self.solution_iterates.append(u_c)
            self.i += 1

        self.solution = u_c
        return u_c

    def animate(
        self,
        filename=None,
        preview=True,
        graph=False,
        duration=0.05,
        plot_function=None,
        r_range=False,
        progress_bar=False,
        target_function=None,
        y_bounds=None,
        **kwargs,
    ):
        if plot_function is None:
            plot_function = self.get_default_plotter(graph=graph, r_range=r_range)
        # for now without graph
        if filename:
            folder = os.path.dirname(filename).replace("\\", "/") + "/"
            file_base = os.path.basename(filename)

        else:
            now = datetime.now()  # current date and time
            time = now.strftime("%H.%M.%S")
            date = now.strftime(r"%d-%m-%Y")
            folder = f"animations/{date}/"
            file_base = f"movie_{time}.gif"

        root_dir = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/") + "/"
        absolute_folder = root_dir + folder
        frame_folder = absolute_folder + "frames/"
        try:
            os.makedirs(frame_folder)
        except FileExistsError:
            pass

        absolute_filename = absolute_folder + file_base

        images = []
        if progress_bar:
            pb = ProgressBar(len(self.solution_iterates), width=30)
        for i in range(len(self.solution_iterates)):
            plot_function(i)
            if target_function is not None:
                x = np.linspace(self.x_l, self.x_r, 1000)
                plt.plot(x, target_function(x), "--")
            if y_bounds is not None:
                plt.ylim(*y_bounds)
            plt.savefig(frame_folder + f"frame_{i}.png")
            images.append(imageio.imread(frame_folder + f"frame_{i}.png"))
            plt.clf()
            if progress_bar:
                pb.tick()

        shutil.rmtree(frame_folder)
        imageio.mimsave(absolute_filename, images, duration=duration)

        if preview:
            webbrowser.open("file://" + absolute_filename)

    def calculate_A_of_segment(self, a_left, a_right):
        return np.array([a_left**2, a_right**2, a_left * a_right, a_left, a_right])

    def calculate_A(self, a):
        return [
            self.calculate_A_of_segment(a_left, a_right)
            for a_left, a_right in zip(a, a[1:])
        ]

    def Pi_functional(self, a):
        total = 0
        for S_i, A_i in zip(self.S, self.calculate_A(a)):
            total += np.sum(S_i * A_i)
        return total

    def compute_J_tilde_of_segment(self, n, v_values_prev, v_values_current):
        """Computes J tilde for the n-th element graph

        Args:
            n (int): index of element (1 up to & incl N)
            S (N by 5 array): the S vectors of the problem description
            v_values_prev (length 3 array): allowed values of node n-1
            v_values_current (length 3 array): allowed values of node n

        Returns:
            3x3 numpy array: J tilde of n-th element
        """
        # n starting from 1
        matrix = []
        b = []
        for i in range(1, 4):  # we loop over all 9 combinations of (a_i, a_{i+1})
            for j in range(1, 4):  # given the 3 allowed values at each nodes
                row = []
                q_i = index_to_q_triplet[i]
                q_j = index_to_q_triplet[j]
                for k in range(3):
                    for l in range(3):
                        row.append(q_i[k] * q_j[l])
                matrix.append(row)

                b.append(
                    np.sum(
                        self.calculate_A_of_segment(
                            v_values_prev[i - 1], v_values_current[j - 1]
                        )
                        * self.S[n - 1]
                    )
                )

        J_vector = np.linalg.solve(matrix, b)  # do we need to transpose the matrix?
        return J_vector.reshape((3, 3))

    def compute_all_J_tildes(self, u_c, r):
        """Computes the J tilde matrices for each of the element graphs.
        (Note that this differs from compute_J_tilde in that it computes the
        allowed values for each node given u_c and r, whereas the latter asks
        you to give the allowed values)

        Args:
            S (N by 5 array): the S vectors of the problem description
            u_c (length N+1 array): current best solution
            r (float): slack variable

        Returns:
            List of n 3x3 numpy arrays: the J tilde matrices
        """
        N = len(u_c) - 1
        j_tildes = []
        for i in range(1, N + 1):
            prev_node = u_c[i - 1]
            curr_node = u_c[i]
            new_j_tilde = self.compute_J_tilde_of_segment(
                i,
                [prev_node - r, prev_node, prev_node + r],
                [curr_node - r, curr_node, curr_node + r],
            )
            j_tildes.append(new_j_tilde)
        return j_tildes


class SADiffEqn(DiffEqn):
    def __init__(
        self,
        p,
        q,
        f,
        initial_condition,
        nodes,
        x_l=0,
        x_r=1,
        boundary_condition="d",
        basis_functions="triangle",
    ) -> None:

        if isinstance(nodes, int):
            nodes = np.linspace(x_l, x_r, nodes + 1)

        S = calculate_S(nodes, basis_functions, p=p, q=q, f=f)
        DiffEqn.__init__(
            self, initial_condition, nodes, S, boundary_condition, basis_functions
        )
        self.x_l = x_l
        self.x_r = x_r


class OffsetSADiffEqn(SADiffEqn):  # template class for Neumann and two sided Neumann
    def __init__(
        self,
        p,
        q,
        f,
        initial_condition,
        nodes,
        offset,
        x_l=0,
        x_r=1,
        boundary_condition="d",
        basis_functions="triangle",
    ) -> None:

        if isinstance(p, (int, float)):
            p_val = p
            p = lambda x: p_val
        if isinstance(q, (int, float)):
            q_val = q
            q = lambda x: q_val
        if isinstance(f, (int, float)):
            f_val = f
            f = lambda x: f_val

        self.offset = np.vectorize(offset, otypes=[float])
        # p_prime = lambda x: derivative(p, x, 0.00001)
        offset_prime = lambda x: derivative(self.offset, x, 0.00001)
        p_offset_prime_prime = lambda x: derivative(
            lambda x: p(x) * offset_prime(x), x, 0.00001
        )
        adjusted_f = lambda x: f(x) + p_offset_prime_prime(x) - self.offset(x) * q(x)

        super().__init__(
            p,
            q,
            adjusted_f,
            initial_condition - self.offset(nodes),
            nodes,
            x_l,
            x_r,
            boundary_condition,
            basis_functions,
        )

    def solve(
        self,
        r,
        r_min=None,
        Pi_min=None,
        sampler=None,
        H=1,
        J_hat=1,
        b_c_strength=1,
        sampler_config=None,
        maximize=False,
        progress_bar=False,
        maxiter=math.inf,
        r_factor=2,
    ):
        soln = super().solve(
            r,
            r_min,
            Pi_min,
            sampler,
            H,
            J_hat,
            b_c_strength,
            sampler_config,
            maximize,
            progress_bar,
            maxiter,
            r_factor,
        )
        node_offsets = self.offset(self.nodes)
        self.solution_iterates = [u_c + node_offsets for u_c in self.solution_iterates]
        return soln + node_offsets


class NeumannSADiffEqn(OffsetSADiffEqn):
    def __init__(
        self,
        p,
        q,
        f,
        initial_condition,
        nodes,
        neumann_value,
        neumann_side,
        x_l=0,
        x_r=1,
        basis_functions="triangle",
    ) -> None:
        if neumann_side.lower() == "r":
            boundary_condition = "d-"
        elif neumann_side.lower() == "l":
            boundary_condition = "-d"

        offset = lambda x: neumann_value * x

        super().__init__(
            p,
            q,
            f,
            initial_condition,
            nodes,
            offset,
            x_l,
            x_r,
            boundary_condition,
            basis_functions,
        )


class DoubleNeumannSADiffEqn(OffsetSADiffEqn):
    def __init__(
        self,
        p,
        q,
        f,
        initial_condition,
        neumann_value_left,
        neumann_value_right,
        nodes,
        x_l=0,
        x_r=1,
        basis_functions="triangle",
    ) -> None:
        boundary_condition = "--"
        c1 = (neumann_value_left - neumann_value_right) / (x_l - x_r) / 2
        c2 = (neumann_value_right * x_l - neumann_value_left * x_r) / (x_l - x_r)
        offset = lambda x: c1 * x**2 + c2 * x
        super().__init__(
            p,
            q,
            f,
            initial_condition,
            nodes,
            offset,
            x_l,
            x_r,
            boundary_condition,
            basis_functions,
        )


class LagrangeDiffEqn(DiffEqn):
    def __init__(
        self,
        alpha,
        initial_condition,
        nodes,
        boundary_condition="D",
        basis_functions="triangle",
        x_l=0,
        x_r=1,
    ) -> None:
        if isinstance(alpha, np.ndarray):
            b = copy.deepcopy(alpha)
            alpha = lambda x: b
        self.n_max, self.m_max = alpha(0).shape
        if isinstance(nodes, int):
            nodes = np.linspace(x_l, x_r, nodes + 1)
        S = self.calculate_S(nodes, basis_functions, alpha)
        print(S[0][3, 0])
        print(S[0][0, 2])
        DiffEqn.__init__(
            self,
            initial_condition,
            nodes,
            S,
            boundary_condition,
            basis_functions,
        )

    def calculate_S(self, nodes, basis_functions_shape, alpha):
        # n,m,k,l
        phi = BasisFunctionsArray(nodes, basis_functions_shape)
        N = len(nodes) - 1

        def phi_deriv(x, i):
            return derivative(phi[i], x, 0.00001)

        n_max, m_max = self.n_max, self.m_max
        s = np.zeros((n_max, m_max, n_max, m_max))

        def phi_array(x, i):
            arr = np.zeros((n_max, m_max, n_max, m_max))
            for n in range(n_max):
                for m in range(m_max):
                    for k in range(n + 1):
                        for l in range(m + 1):
                            arr[n, m, k, l] = (
                                phi[i](x) ** k
                                * phi[i + 1](x) ** (n - k)
                                * phi_deriv(x, i) ** l
                                * phi_deriv(x, i + 1) ** (m - l)
                            )
            return arr

        list_of_S = []
        for i in range(N):
            array_valued_integrand = lambda x: np.expand_dims(
                alpha(x), (2, 3)
            ) * phi_array(x, i)
            s = integrate.quad_vec(array_valued_integrand, nodes[i], nodes[i + 1])[0]
            list_of_S.append(s)

        return list_of_S

    def calculate_A_of_segment(
        self, a_left, a_right
    ):  # i-th segment, normally n for segment but was taken
        n_max, m_max = self.n_max, self.m_max
        arr = np.zeros((n_max, m_max, n_max, m_max))
        for n in range(n_max):
            for m in range(m_max):
                for k in range(n + 1):
                    for l in range(m + 1):
                        arr[n, m, k, l] = (
                            a_left ** (k + l)
                            * a_right ** (n + m - k - l)
                            * comb(n, k)
                            * comb(m, l)  # choose function
                        )
        return arr
