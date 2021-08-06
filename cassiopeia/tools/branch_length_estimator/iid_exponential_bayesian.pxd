from libcpp.vector cimport vector
from libcpp.map cimport map
from libcpp.pair cimport pair

# Declare the class with cdef
cdef extern from "iid_exponential_bayesian_cpp.h":
    cdef cppclass DP:
        DP() except +
        void run(
            int N,
            vector[vector[int]] children,
            int root,
            vector[int] is_internal_node,
            vector[int] get_number_of_mutated_characters_in_node,
            vector[int] non_root_internal_nodes,
            vector[int] leaves,
            vector[int] parent,
            int K,
            vector[int] Ks,
            int T,
            double r,
            double lam,
            double sampling_probability,
            vector[int] is_leaf,
        ) except +
        vector[pair[vector[int], double]] get_down_res()
        vector[pair[vector[int], double]] get_up_res()
        vector[pair[int, double]] get_posterior_means_res()
        vector[pair[int, vector[double]]] get_posteriors_res()
        vector[pair[int, vector[double]]] get_log_joints_res()
        double get_log_likelihood_res()
