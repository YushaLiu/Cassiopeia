"""
This file stores a subclass of GreedySolver, the MaxCutSolver. This subclass 
implements an inference procedure inspired by Snir and Rao (2006) that 
approximates the max-cut problem on a connectivity graph generated from the 
observed mutations on a group of samples. The connectivity graph represents a
supertree generated over phylogenetic trees for each individual character. The
goal is to find a partition on the graph that resolves triplets on the 
supertree, grouping together samples that share mutations and seperating
samples that differ in mutations.
"""
import itertools
import networkx as nx
import numpy as np
import pandas as pd
from typing import Callable, Dict, List, Optional, Tuple, Union

from cassiopeia.solver import GreedySolver
from cassiopeia.solver import graph_utilities


class MaxCutSolver(GreedySolver.GreedySolver):
    """The MaxCutSolver implements a top-down algorithm that recursively
    partitions the sample set based on connectivity. At each recursive step,
    a connectivity graph is generated for the sample set, where shared
    mutations are coded as strong negative connections and differing mutations
    are coded as positive connections. Then a partition is generated by finding
    a maximum weight cut over the graph. The final partition is also improved
    upon by a greedy hill-climbing procedure that further improves the cut.

    Args:
        character_matrix: A character matrix of observed character states for
            all samples
        missing_char: The character representing missing values
        meta_data: Any meta data associated with the samples
        priors: Prior probabilities of observing a transition from 0 to any
            character state
        sdimension: The number of dimensions to use for the embedding space.
            Acts as a hyperparameter
        iterations: The number of iterations in updating the embeddings.
            Acts as a hyperparameter
        weights: A set of optional weights for edges in the connectivity graph

    Attributes:
        character_matrix: The character matrix describing the samples
        missing_char: The character representing missing values
        meta_data: Data table storing meta data for each sample
        priors: Prior probabilities of character state transitions
        tree: The tree built by `self.solve()`. None if `solve` has not been
            called yet
        prune_cm: A character matrix with duplicate rows filtered out, removing
            doublets from the sample set
        sdimension: The number of dimensions to use for the embedding space
        iterations: The number of iterations in updating the embeddings
        weights: A set of optional weights for edges in the connectivity graph
    """

    def __init__(
        self,
        character_matrix: pd.DataFrame,
        missing_char: str,
        meta_data: Optional[pd.DataFrame] = None,
        priors: Optional[Dict] = None,
        sdimension: Optional[int] = 3,
        iterations: Optional[int] = 50,
        weights: Optional[Dict] = None,
    ):

        super().__init__(character_matrix, missing_char, meta_data, priors)
        self.sdimension = sdimension
        self.iterations = iterations
        self.weights = weights

    def perform_split(
        self,
        mutation_frequencies: Dict[int, Dict[str, int]],
        samples: List[int] = None,
    ) -> Tuple[List[int], List[int]]:
        """Generate a partition of the samples.

        First, a connectivity graph is generated with samples as nodes such
        that samples with shared mutations have strong negative connectivity
        and samples with distant mutations have positive connectivity. Then,
        the algorithm finds a partition by using a heuristic method to find
        the max-cut on the connectivity graph. The samples are randomly
        embedded in a d-dimensional sphere and the embeddings for each node
        are iteratively updated based on neighboring edge weights in the
        connectivity graph such that nodes with stronger connectivity cluster
        together. The final partition is generated by choosing random
        hyperplanes to bisect the d-sphere and taking the one that maximizes
        the cut.

        Args:
            mutation_frequencies: A dictionary containing the frequencies of
                each character/state pair that appear in the character matrix
                restricted to the sample set
            samples: A list of samples to partition

        Returns:
            A tuple of lists, representing the left and right partitions
        """
        G = graph_utilities.construct_connectivity_graph(
            self.prune_cm,
            mutation_frequencies,
            self.missing_char,
            samples,
            w=self.weights,
        )

        embedding_dimension = self.sdimension + 1
        emb = {}
        for i in G.nodes():
            x = np.random.normal(size=embedding_dimension)
            x = x / np.linalg.norm(x)
            emb[i] = x

        for _ in range(self.iterations):
            new_emb = {}
            for i in G.nodes:
                cm = np.zeros(embedding_dimension, dtype=float)
                for j in G.neighbors(i):
                    cm -= (
                        G[i][j]["weight"]
                        * np.linalg.norm(emb[i] - emb[j])
                        * emb[j]
                    )
                cm = cm / np.linalg.norm(cm)
                new_emb[i] = cm
            emb = new_emb

        return_cut = []
        best_score = 0
        for _ in range(3 * embedding_dimension):
            b = np.random.normal(size=embedding_dimension)
            b = b / np.linalg.norm(b)
            cut = []
            for i in G.nodes():
                if np.dot(emb[i], b) > 0:
                    cut.append(i)
            this_score = self.evaluate_cut(cut, G)
            if this_score > best_score:
                return_cut = cut
                best_score = this_score

        improved_cut = graph_utilities.max_cut_improve_cut(G, return_cut)

        rest = set(samples) - set(improved_cut)

        return improved_cut, list(rest)

    def evaluate_cut(self, cut: List[int], G: nx.DiGraph) -> float:
        """A simple function to evaluate the weight of a cut.

        For each edge in the graph, checks if it is in the cut, and then adds its
        edge weight to the cut if it is.

        Args:
            cut: A list of nodes that represents one of the sides of a cut
                on the graph
            G: The graph the cut is over
        Returns:
            The weight of the cut
        """
        cut_score = 0
        for e in G.edges():
            u = e[0]
            v = e[1]
            w_uv = G[u][v]["weight"]
            if graph_utilities.check_if_cut(u, v, cut):
                cut_score += float(w_uv)

        return cut_score
