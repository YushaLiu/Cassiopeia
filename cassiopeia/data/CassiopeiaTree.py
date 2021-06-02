"""
This file stores the basic data structure for Cassiopeia - the
CassiopeiaTree. This data structure will typically contain a character
matrix containing that character state information for all the cells in a given
clonal population (though this is not required). Other important data is also
stored here, like the priors for given character states as well any meta data
associated with this clonal  population.

When a solver has been called on this object, a tree will be added to the data
structure at which point basic properties can be queried like the average tree
depth or agreement between character states and phylogeny.

This object can be passed to any CassiopeiaSolver subclass as well as any
analysis module, like a branch length estimator or rate matrix estimator
"""
import copy
import itertools
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union
import warnings

import collections
import ete3
import networkx as nx
import numpy as np
import pandas as pd
import scipy

from cassiopeia.data import utilities
from cassiopeia.solver import solver_utilities


class CassiopeiaTreeError(Exception):
    """An Exception class for the CassiopeiaTree class."""

    pass


class CassiopeiaTreeWarning(UserWarning):
    """A Warning for the CassiopeiaTree class."""

    pass


class CassiopeiaTree:
    """Basic tree object for Cassiopeia.

    This object stores the key attributes and functionalities a user might want
    for working with lineage tracing experiments. At its core, it stores
    three main items - a tree, a character matrix, and meta data associated
    with the data.

    The tree can be fed into the object via Ete3, Networkx, or can be inferred
    using one of the CassiopeiaSolver algorithms in the `solver` module. The
    tree here is only used for obtaining the _topology_ of the tree.

    A character matrix can be stored in the object, containing the states
    observed for each cell. In typical lineage tracing experiments, these are
    integer representations of the indels observed at each unique cut site. We
    track both an unmodified version of the character matrix (obtainable via
    the `get_original_character_matrix` method) that does not maintain
    consistency with the character states of the leaves, and a working character
    matrix (obtainable via the `get_current_character_matrix` method) that
    is updated when the character states of leaves are changed.

    Some reconstruction algorithms will make use of dissimilarities between
    cells. To this end, we store these `dissimilarity maps` in the
    CassiopeiaTree class itself. For users trying to diagnose the reconstruction
    accuracy with a known groundtruth, they can compare this dissimilarity
    map to the phylogenetic distance on the tree.

    Meta data for cells or characters can also be stored in this object. These
    items can be categorical or numerical in nature. Common examples of cell
    meta data are the cluster identity, tissue identity, or number of target-site
    UMIs per cell. These items can be used in downstream analyses, for example
    the FitchCount algorithm which infers the number of transitions between
    categorical variables (e.g., tissues). Common examples of character meta
    data are the proportion of missing data for each character or the entropy
    of states. These are good statistics to have for feature selection.

    TODO(rzhang): Add check upon initialization that input tree is valid tree.
    TODO(mattjones315): Add experimental meta data as arguments.
    TODO(mattjones315): Add utility methods to compute the colless index
        and the cophenetic correlation wrt to some cell meta item
    TODO(mattjones315): Add bulk set_states method.
    TODO(mattjones): Add boolean to `get_tree_topology` which will include
        all attributes (e.g., node times)

    Args:
        character_matrix: The character matrix for the lineage.
        missing_state_indicator: An indicator for missing states in the
            character matrix.
        cell_meta: Per-cell meta data
        character_meta: Per-character meta data
        priors: A dictionary storing the probability of a character mutating
            to a particular state.
        tree: A tree for the lineage.
        dissimilarity_map: An NxN dataframe storing the pairwise dissimilarities
            between samples.
        root_sample_name: The name of the sample to treat as the root. This
            is not always used, but will be added if needed during tree
            reconstruction. If the user already has a sample in the character
            matrix or dissimilarity map that they would like to use as the
            phylogenetic root, they can specify it here.
    """

    def __init__(
        self,
        character_matrix: Optional[pd.DataFrame] = None,
        missing_state_indicator: int = -1,
        cell_meta: Optional[pd.DataFrame] = None,
        character_meta: Optional[pd.DataFrame] = None,
        priors: Optional[Dict[int, Dict[int, float]]] = None,
        tree: Optional[Union[str, ete3.Tree, nx.DiGraph]] = None,
        dissimilarity_map: Optional[pd.DataFrame] = None,
        root_sample_name: Optional[str] = None,
    ) -> None:

        self.missing_state_indicator = missing_state_indicator
        self.cell_meta = cell_meta
        self.character_meta = character_meta
        self.priors = priors
        self.__network = None
        self.__cache = {}

        self.__original_character_matrix = None
        self.__current_character_matrix = None
        if character_matrix is not None:
            self.set_character_matrix(character_matrix)

        if tree is not None:
            tree = copy.deepcopy(tree)
            self.populate_tree(tree)

        # these attributes are helpful for distance based solvers
        self.__dissimilarity_map = None
        if dissimilarity_map is not None:
            self.set_dissimilarity_map(dissimilarity_map)
        self.root_sample_name = root_sample_name

    def populate_tree(self, tree: Union[str, ete3.Tree, nx.DiGraph]) -> None:

        if isinstance(tree, nx.DiGraph):
            self.__network = tree
        elif isinstance(tree, str):
            self.__network = utilities.newick_to_networkx(tree)
        elif isinstance(tree, ete3.Tree):
            self.__network = utilities.ete3_to_networkx(tree)
        else:
            raise CassiopeiaTreeError(
                "Please pass an ete3 Tree, a newick string, or a Networkx object."
            )

        # enforce all names to be strings
        rename_dictionary = {}
        for n in self.__network.nodes:
            rename_dictionary[n] = str(n)

        self.__network = nx.relabel_nodes(self.__network, rename_dictionary)

        # clear cache if we're changing the topology of the tree
        self.__cache = {}

        # add character states
        for n in self.nodes:
            if (
                self.__original_character_matrix is not None
                and n in self.__original_character_matrix.index.tolist()
            ):
                self.__network.nodes[n][
                    "character_states"
                ] = self.__original_character_matrix.loc[n].to_list()
            else:
                self.__network.nodes[n]["character_states"] = []

        # instantiate branch lengths
        for u, v in self.edges:
            self.__network[u][v]["length"] = 1

        # instantiate node time
        self.__network.nodes[self.root]["time"] = 0
        for u, v in self.depth_first_traverse_edges(source=self.root):
            self.__network.nodes[v]["time"] = (
                self.__network.nodes[u]["time"] + self.__network[u][v]["length"]
            )

    def __check_network_initialized(self) -> None:
        if self.__network is None:
            raise CassiopeiaTreeError("Tree has not been initialized.")

    def set_character_matrix(self, character_matrix: pd.DataFrame):
        """Initializes a character matrix in the object."""

        if not all(type(i) == str for i in character_matrix.index):
            raise CassiopeiaTreeError("Index of character matrix must consist"
            " of strings.")

        self.__original_character_matrix = character_matrix.copy()
        self.__current_character_matrix = character_matrix.copy()

        # overwrite character information at the leaves if needed
        if self.__network:
            self.initialize_character_states_at_leaves(character_matrix)

    def initialize_character_states_at_leaves(
        self, character_matrix: Union[pd.DataFrame, Dict]
    ) -> None:
        """Populates character states at leaves.

        Assigns character states to the leaves of the tree. This function
        must have a character state assignment to all leaves of the tree.

        Args:
            character_matrix: A pandas dataframe or dictionary for mapping
                character states to the leaves of the tree.

        Raises:
            CassiopeiaTreeError if not all leaves are accounted for or if the
                tree has not been initialized.
        """
        self.__check_network_initialized()

        if isinstance(character_matrix, dict):
            character_matrix = pd.DataFrame.from_dict(
                character_matrix, orient="index"
            )

        if set(self.leaves) != set(character_matrix.index.values):
            raise CassiopeiaTreeError(
                "Character matrix index does not match set of leaves."
            )

        for n in self.leaves:
            self.__set_character_states(n, character_matrix.loc[n].tolist())

        self.__original_character_matrix = character_matrix.copy()
        self.__current_character_matrix = character_matrix.copy()

    def initialize_all_character_states(
        self, character_state_mapping: Dict
    ) -> None:
        """Populates character states across the tree.

        Assigns character states to all of the nodes in the tree. The mapping
        must have an entry for every node in the tree.

        Args:
            character_state_mapping: A mapping containing character state
                assignments for every node

        Raises:
            CassiopeiaTreeError if the tree is not initialized or if the
                character_state_mapping does not contain assignments for every
                node.
        """
        self.__check_network_initialized()

        if set([n for n in character_state_mapping.keys()]) != set(self.nodes):
            raise CassiopeiaTreeError(
                "Mapping does not account for all the nodes."
            )

        character_matrix = {}
        for n in self.nodes:
            if self.is_leaf(n):
                character_matrix[n] = character_state_mapping[n]
            self.__set_character_states(n, character_state_mapping[n])

        character_matrix = pd.DataFrame.from_dict(
            character_matrix, orient="index"
        )
        self.__original_character_matrix = character_matrix.copy()
        self.__current_character_matrix = character_matrix.copy()

    def get_original_character_matrix(self) -> pd.DataFrame:
        """Gets the original character matrix.

        The returned character matrix is the original character matrix of
        observations. Downstream operations might change the character state
        observations for the cells and if this happens, the changes will
        not be reflected here. Instead, the changes will be reflected in the
        character matrix obtained with `get_current_character_matrix`.

        Returns:
            A copy of the original, unmodified character matrix.

        Raises:
            CassiopeiaTreeError if the character matrix does not exist.
        """
        if self.__original_character_matrix is None:
            raise CassiopeiaTreeError("Character matrix does not exist.")
        return self.__original_character_matrix.copy()

    def get_current_character_matrix(self) -> pd.DataFrame:
        """Gets the current character matrix.

        The returned character matrix is the modified character matrix of
        observations. When downstream operations are used to change the
        character state observations in the leaves of the tree, these changes
        will be reflected here. A "raw" version of the character matrix can
        be found in the `get_original_character_matrix` method.

        Returns:
            A copy of the modified character matrix.

        Raises:
            CassiopeiaTreeError if the character matrix does not exist.
        """
        if self.__current_character_matrix is None:
            raise CassiopeiaTreeError("Character matrix does not exist.")
        return self.__current_character_matrix.copy()

    @property
    def n_cell(self) -> int:
        """Returns number of cells in tree.

        Raises:
            CassiopeiaTreeError if the object is empty (i.e. no tree or
            character matrix).
        """
        if self.__current_character_matrix is None:
            if self.__network is None:
                raise CassiopeiaTreeError(
                    "This is an empty object with no tree or character matrix."
                )
            return len(self.leaves)
        return self.__current_character_matrix.shape[0]

    @property
    def n_character(self) -> int:
        """Returns number of characters in character matrix.

        Raises:
            CassiopeiaTreeError if the object is empty (i.e. no tree or
            character matrix) or if the character states have not been
            initialized.
        """
        if self.__current_character_matrix is None:
            if self.__network is None:
                raise CassiopeiaTreeError(
                    "This is an empty object with no tree or character matrix."
                )
            if "character_states" in self.__network.nodes[self.leaves[0]]:
                return len(self.get_character_states(self.leaves[0]))
            raise CassiopeiaTreeError(
                "Character states have not been initialized."
            )
        return self.__current_character_matrix.shape[1]

    @property
    def root(self) -> str:
        """Returns root of tree.

        Returns:
            The root.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "root" not in self.__cache:
            self.__cache["root"] = [
                n for n in self.__network if self.is_root(n)
            ][0]
        return self.__cache["root"]

    @property
    def leaves(self) -> List[str]:
        """Returns leaves of tree.

        Returns:
            The leaves of the tree.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "leaves" not in self.__cache:
            self.__cache["leaves"] = [
                n for n in self.__network if self.is_leaf(n)
            ]
        return self.__cache["leaves"][:]

    @property
    def internal_nodes(self) -> List[str]:
        """Returns internal nodes in tree (including the root).

        Returns:
            The internal nodes of the tree (i.e. all nodes not at the leaves)

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "internal_nodes" not in self.__cache:
            self.__cache["internal_nodes"] = [
                n for n in self.__network if self.is_internal_node(n)
            ]
        return self.__cache["internal_nodes"][:]

    @property
    def nodes(self) -> List[str]:
        """Returns all nodes in tree.

        Returns:
            All nodes of the tree (internal + leaves)

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "nodes" not in self.__cache:
            self.__cache["nodes"] = [n for n in self.__network]
        return self.__cache["nodes"][:]

    @property
    def edges(self) -> List[Tuple[str, str]]:
        """Returns all edges in the tree.

        Returns:
            All edges of the tree.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "edges" not in self.__cache:
            self.__cache["edges"] = [(u, v) for (u, v) in self.__network.edges]
        return self.__cache["edges"][:]

    def is_leaf(self, node: str) -> bool:
        """Returns whether or not the node is a leaf.

        Returns:
            Whether or not the node is a leaf.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()
        return self.__network.out_degree(node) == 0

    def is_root(self, node: str) -> bool:
        """Returns whether or not the node is the root.

        Returns:
            Whether or not the node is the root.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()
        return self.__network.in_degree(node) == 0

    def is_internal_node(self, node: str) -> bool:
        """Returns whether or not the node is an internal node.

        Returns:
            Whether or not the node is an internal node (i.e. out degree is
            greater than 0). In this case, the root is considered an internal
            node.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()
        return self.__network.out_degree(node) > 0

    def is_ambiguous(self, node: str) -> bool:
        """Returns whether the node is ambiguous. This is detected by checking if
        any of the characters are tuples.

        Returns:
            True if the node is ambiguous, False otherwise.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()
        states = self.get_character_states(node)
        return any(isinstance(state, tuple) for state in states)

    def collapse_ambiguous_characters(self) -> None:
        """Only retain unique characters for ambiguous nodes. Usually, ambiguous
        nodes have character strings represented as a list of tuples of integers.
        The inner list may contain multiple of the same states, encoding the
        relative abundance of a certain state in the ambiguous state distribution.
        Calling this function removes such duplicates and only retains unique
        characters. This function is idempotent and does nothing for trees that
        have no ambiguous characters.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        for node in self.nodes:
            states = self.get_character_states(node)
            # Modify states in place. This is okay because get_character_states
            # returns a copy.
            modified = False
            for i in range(len(states)):
                if isinstance(states[i], tuple):
                    new_states = tuple(set(states[i]))
                    if states != new_states:
                        states[i] = new_states
                        modified = True
            if modified:
                self.set_character_states(node, states)

    def resolve_ambiguous_characters(
        self, resolve_function: Optional[Callable[[List[int]], int]] = None
    ) -> None:
        """Resolve all nodes with ambiguous characters. A custom ``resolve_function``
        may be provided to perform the resolution. By default, the most abundant
        state is selected. One is randomly selected on ties. Modifies the tree in-place.

        Args:
            resolve_function: Function that performs character resolution. This
                function is called once per ambiguous character state, and thus
                takes a single integer list as its argument and returns the
                resolved character state.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        for node in self.nodes:
            states = self.get_character_states(node)

            # Modify states in place. This is okay because get_character_states
            # returns a copy.
            modified = False
            for i in range(len(states)):
                if isinstance(states[i], tuple):
                    if resolve_function:
                        new_state = resolve_function(states[i])
                    else:
                        most_common = collections.Counter(states[i]).most_common()
                        new_state = np.random.choice([
                            state
                            for state, count in most_common
                            if count == most_common[0][1]
                        ])

                    states[i] = new_state
                    modified = True
            if modified:
                self.set_character_states(node, states)

        # All nodes have a single character string, so we cast the character matrix
        # back to an integer.
        self.__current_character_matrix = self.__current_character_matrix.astype(int, copy=False)


    def reconstruct_ancestral_characters(self) -> None:
        """Reconstruct ancestral character states.

        Reconstructs ancestral states (i.e., those character states in the
        internal nodes) using the Camin-Sokal parsimony criterion (i.e.,
        irreversibility). Operates on the tree in place.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        for n in self.depth_first_traverse_nodes(postorder=True):
            if self.is_leaf(n):
                if len(self.get_character_states(n)) == 0:
                    raise CassiopeiaTreeError("Character states not annotated "
                    "at a leaf node, initialize character states at leaves "
                    "before reconstructing ancestral characters."
                    )
                continue
            children = self.children(n)
            character_states = [self.get_character_states(c) for c in children]
            reconstructed = utilities.get_lca_characters(
                character_states, self.missing_state_indicator
            )
            self.__set_character_states(n, reconstructed)

    def parent(self, node: str) -> str:
        """Gets the parent of a node.

        Args:
            node: A node in the tree

        Returns:
            The parent of the node.

        Raises:
            CassiopeiaTreeError if the tree is not initialized.
        """
        self.__check_network_initialized()

        return [u for u in self.__network.predecessors(node)][0]

    def children(self, node: str) -> List[str]:
        """Gets the children of a given node.

        Args:
            node: A node in the tree.

        Returns:
            A list of nodes that are direct children of the input node.

        Raises:
            CassiopeiaTreeError if the tree is not initialized.
        """
        self.__check_network_initialized()
        return [v for v in self.__network.successors(node)]

    def __remove_node(self, node) -> None:
        """Private method to remove node from tree.

        Args:
            node: A node in the tree to be removed

        Raises:
            CassiopeiaTreeError if the tree is not initialized.
        """
        self.__check_network_initialized()

        self.__network.remove_node(node)

    def __add_node(self, node) -> None:
        """Private method to add node to tree.

        Args:
            node: A node to be added to the tree.

        Raises:
            CassiopeiaTreeError if the tree is not initialized.
        """
        self.__check_network_initialized()

        self.__network.add_node(node)

    def __remove_edge(self, u, v) -> None:
        """Private method to remove edge from tree.

        Args:
            u: The source node of the directed edge to be removed
            v: The sink node of the directed edge to be removed

        Raises:
            CassiopeiaTreeError if the tree is not initialized.
        """
        self.__check_network_initialized()

        self.__network.remove_edge(u, v)

    def __add_edge(self, u, v) -> None:
        """Private method to add edge to tree.

        Args:
            u: The source node of the directed edge to be added
            v: The sink node of the directed edge to be added

        Raises:
            CassiopeiaTreeError if the tree is not initialized.
        """
        self.__check_network_initialized()

        self.__network.add_edge(u, v)

    def set_time(self, node: str, new_time: float) -> None:
        """Sets the time of a node.

        Importantly, this maintains consistency with the rest of the tree. In
        other words, setting the time of a particular node will change the
        length of the edge leading into the node and the edges leading out. This
        function requires monotonicity of times are maintained (i.e. no negative
        branch lengths).

        Args:
            node: Node in the tree
            new_time: New time for the node.

        Raises:
            CassiopeiaTreeError if the tree is not initialized, if the new
                time is less than the time of the parent, or if monotonicity
                is not maintained.
        """
        self.__check_network_initialized()

        if not self.is_root(node):
            parent = self.parent(node)
            if new_time < self.get_time(parent):
                raise CassiopeiaTreeError(
                    "New age is less than the age of the parent."
                )

        for child in self.children(node):
            if new_time > self.get_time(child):
                raise CassiopeiaTreeError(
                    "New age is greater than than a child."
                )

        self.__network.nodes[node]["time"] = new_time

        self.__network[parent][node]["length"] = new_time - self.get_time(
            parent
        )
        for child in self.children(node):
            self.__network[node][child]["length"] = (
                self.get_time(child) - new_time
            )

    def set_times(self, time_dict: Dict[str, float]) -> None:
        """Sets the time of all nodes in the tree.

        Importantly, this maintains consistency with the rest of the tree. In
        other words, setting the time of all nodes will change the length of
        the edges too. This function requires monotonicity of times are
        maintained (i.e. no negative branch lengths).

        Args:
            time_dict: Dictionary mapping nodes to their time.

        Raises:
            CassiopeiaTreeError if the tree is not initialized, or if the time
            of any parent is greater than that of a child.
        """
        self.__check_network_initialized()

        # TODO: Check that the keys of time_dict match exactly the nodes in the
        # tree and raise otherwise?
        # Currently, if nodes are missing in time_dict, code below blows up. If
        # extra nodes are present, they are ignored.

        for (parent, child) in self.edges:
            time_parent = time_dict[parent]
            time_child = time_dict[child]
            if time_parent > time_child:
                raise CassiopeiaTreeError(
                    "Time of parent greater than that of child: "
                    f"{time_parent} > {time_child}"
                )
            self.__network[parent][child]["length"] = time_child - time_parent
        for node, time in time_dict.items():
            self.__network.nodes[node]["time"] = time

    def get_time(self, node: str) -> float:
        """Gets the time of a node.

        Returns the time of a node, defined as the sum of edge lengths from the
        root to the node.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        return self.__network.nodes[node]["time"]

    def get_times(self) -> Dict[str, float]:
        """Gets the times of all nodes.

        Returns the times of all nodes, defined as the sum of edge lengths from
        the root to that node.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        return dict([(node, self.get_time(node)) for node in self.nodes])

    def __set_branch_length(self, parent: str, child: str, length: float) -> None:
        """A private method for setting branch lengths.

        A private method for setting branch lengths with no checks. Useful
        for the internal CassiopeiaTree API.

        Args:
            parent: Parent node of the edge
            child: Child node of the edge
            length: New edge length
        """

        self.__network[parent][child]["length"] = length


    def set_branch_length(self, parent: str, child: str, length: float) -> None:
        """Sets the length of a branch.

        Adjusts the branch length of the specified parent-child relationship.
        This procedure maintains the consistency with the rest of the times in
        the tree. Namely, by changing the branch length here, it will change
        the times of all the nodes below the parent of interest, relative to the
        difference between the old and new branch length.

        Args:
            parent: Parent node of the edge
            child: Child node of the edge
            length: New edge length

        Raises:
            CassiopeiaTreeError if the tree is not initialized, if the edge
                does not exist, or if the edge length is negative.
        """
        self.__check_network_initialized()

        if child not in self.children(parent):
            raise CassiopeiaTreeError("Edge does not exist.")

        if length < 0:
            raise CassiopeiaTreeError("Edge length must be positive.")

        self.__set_branch_length(parent, child, length)

        for u, v in self.depth_first_traverse_edges(source=parent):
            self.__network.nodes[v]["time"] = (
                self.__network.nodes[u]["time"] + self.__network[u][v]["length"]
            )

    def set_branch_lengths(self, branch_length_dict: Dict[Tuple[str, str], float]) -> None:
        """Sets the length of multiple branches on a tree.

        Adjusts the branch length of specified parent-child relationships.
        This procedure maintains the consistency with the rest of the times in
        the tree. Namely, by changing branch lengths here, it will change
        the times of all the nodes in the tree such that the times are
        representative of the new branch lengths.

        Args:
            branch_dict: A dictionary of edges to updated branch lengths

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        for edge, length in branch_length_dict.items():
            u, v = edge[0], edge[1]
            if v not in self.children(u):
                raise CassiopeiaTreeError("Edge does not exist.")
            if length < 0:
                raise CassiopeiaTreeError("Edge length must be positive.")
            self.__set_branch_length(u, v, length)

        for u, v in self.depth_first_traverse_edges():
            self.__network.nodes[v]["time"] = (
                self.__network.nodes[u]["time"] + self.__network[u][v]["length"]
            )

    def get_branch_length(self, parent: str, child: str) -> float:
        """Gets the length of a branch.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized or if the
                branch does not exist in the tree.
        """
        self.__check_network_initialized()

        if child not in self.children(parent):
            raise CassiopeiaTreeError("Edge does not exist.")

        return self.__network[parent][child]["length"]

    def set_character_states(self, node: str, states: Union[List[int], List[Tuple[int, ...]]]) -> None:
        """Sets the character states for a particular node.

        Args:
            node: Node in the tree
            states: A list of states to add to the node.

        Raises:
            CassiopeiaTreeError if the character vector is the incorrect length,
                or if the node of interest is a leaf that has not been
                instantiated.
        """
        self.__check_network_initialized()

        if len(states) != self.n_character:
            raise CassiopeiaTreeError(
                "Input character vector is not the right length."
            )

        if self.is_leaf(node):
            if self.get_character_states(node) == []:
                raise CassiopeiaTreeError(
                    "Leaf node character states have not been instantiated"
                )
        self.__set_character_states(node, states)

        if self.is_leaf(node):
            # Cast entire character matrix to object dtype if we get an ambiguous
            # character string.
            if any(isinstance(state, tuple) for state in states):
                self.__current_character_matrix = self.__current_character_matrix.astype(object, copy=False)
            # Pass in as numpy array of tuples to bypass the VisibleDeprecationWarning
            # from creating an ndarray from ragged nested sequences
            states_arr = np.empty(len(states), dtype=object)
            states_arr[:] = states
            self.__current_character_matrix.at[node] = states_arr

    def __set_character_states(self, node: str, states: Union[List[int], List[Tuple[int, ...]]]) -> None:
        """A private method for setting states.

        A private method for setting states of nodes with no checks. Useful
        for the internal CassiopeiaTree API.

        Args:
            node: Node in the tree
            states: A list of states to add to the node.
        """

        self.__network.nodes[node]["character_states"] = states

    def get_character_states(self, node: str) -> Union[List[int], List[Tuple[int, ...]]]:
        """Gets all the character states for a particular node.

        Args:
            node: Node in the tree.

        Returns:
            The full character state array of the specified node.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        return self.__network.nodes[node]["character_states"][:]

    def get_all_ancestors(self, node: str) -> List[str]:
        """Gets all the ancestors of a particular node. Nodes that are closest
        to the given node appear first in the list.

        Args:
            node: Node in the tree

        Returns:
            The list of nodes along the path from the root to the node.
        """

        self.__check_network_initialized()

        if "ancestors" not in self.__cache:
            self.__cache["ancestors"] = {}

        if node not in self.__cache["ancestors"]:
            self.__cache["ancestors"][node] = []

            current_node = node
            while not self.is_root(current_node):
                ancestor = self.parent(current_node)
                self.__cache["ancestors"][node].append(ancestor)
                current_node = ancestor

        return self.__cache["ancestors"][node]

    def depth_first_traverse_nodes(
        self, source: Optional[int] = None, postorder: bool = True
    ) -> Iterator[str]:
        """Nodes from depth first traversal of the tree.

        Returns the nodes from a DFS on the tree.

        Args:
            source: Where to begin the depth first traversal.
            postorder: Return the nodes in postorder. If False, returns in
                preorder.

        Returns:
            A list of nodes from the depth first traversal.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if source is None:
            source = self.root

        if postorder:
            return nx.dfs_postorder_nodes(self.__network, source=source)
        else:
            return nx.dfs_preorder_nodes(self.__network, source=source)

    def depth_first_traverse_edges(
        self, source: Optional[int] = None
    ) -> Iterator[Tuple[str, str]]:
        """Edges from depth first traversal of the tree.

        Returns the edges from a DFS on the tree.

        Args:
            source: Where to begin the depth first traversal.

        Returns:
            A list of edges from the depth first traversal.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """

        self.__check_network_initialized()

        if source is None:
            source = self.root

        return nx.dfs_edges(self.__network, source=source)

    def leaves_in_subtree(self, node) -> List[str]:
        """Get leaves in subtree below a given node.

        Args:
            node: Root of the subtree.

        Returns:
            A list of the leaves in the subtree rooted at the specified node.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "subtree" not in self.__cache:
            self.__cache["subtree"] = {}

            for n in self.depth_first_traverse_nodes(postorder=True):
                if self.is_leaf(n):
                    self.__cache["subtree"][n] = [n]
                else:
                    leaves = []
                    for child in self.children(n):
                        leaves += self.leaves_in_subtree(child)
                    self.__cache["subtree"][n] = leaves

        return self.__cache["subtree"][node]


    def get_newick(self, record_branch_lengths = False) -> str:
        """Returns newick format of tree.

        Args:
            record_branch_lengths: Whether to record branch lengths on the tree
            in the newick string

        Returns:
            The tree in the form of a newick string

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        # No nodes should have a comma in the name. Otherwise, they will be
        # displayed as disconnected nodes. For instance, if there is a node with
        # name "1,2,3", then three separate nodes will be output. We decide to
        # raise an exception instead of a warning because the returned newick string
        # will be a WRONG tree.
        if any(',' in node for node in self.nodes):
            raise CassiopeiaTreeError("No nodes may have the comma (,) character in its name.")

        return utilities.to_newick(self.__network, record_branch_lengths)

    def get_tree_topology(self) -> nx.DiGraph:
        """Returns the tree in Networkx format.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """

        self.__check_network_initialized()

        if self.__network:
            return self.__network.copy()
        else:
            return None

    def get_mean_depth_of_tree(self) -> float:
        """Computes mean depth of tree.

        Returns the mean depth of the tree. If branch lengths have not been
        estimated, depth is by default the number of edges in the tree.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        depths = [self.get_time(l) for l in self.leaves]
        return np.mean(depths)

    def get_max_depth_of_tree(self) -> float:
        """Computes the max depth of the tree.

        Returns the maximum depth of the tree. If branch lengths have not been
        estimated, depth is by default the number of edges in the tree.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        depths = [self.get_time(l) for l in self.leaves]
        return np.max(depths)

    def get_mutations_along_edge(
        self, parent: str, child: str
    ) -> List[Tuple[int, int]]:
        """Gets the mutations along an edge of interest.

        Returns a list of tuples (character, state) of mutations that occur
        along an edge. Characters are 0-indexed.

        Args:
            parent: parent in tree
            child: child in tree

        Returns:
            A list of (character, state) tuples indicating which character
                mutated and to which state.

        Raises:
            CassiopeiaTreeError if the edge does not exist or if the tree is
                not initialized.
        """
        self.__check_network_initialized()

        if child not in self.children(parent):
            raise CassiopeiaTreeError("Edge does not exist.")

        parent_states = self.get_character_states(parent)
        child_states = self.get_character_states(child)

        mutations = []
        for i in range(self.n_character):
            if parent_states[i] != child_states[i]:
                mutations.append((i, child_states[i]))

        return mutations

    def relabel_nodes(self, relabel_map: Dict[str, str]) -> None:
        """Relabels the nodes in the tree.

        Renames the nodes in the tree according to the relabeling map. Modifies
        the tree in-place.

        Args:
            relabel_map: A mapping of old names to new names.

        Raises:
            CassiopeiaTreeError if the tree is not initialized.
        """
        self.__check_network_initialized()

        self.__network = nx.relabel_nodes(self.__network, relabel_map)

        # reset cache because we've changed names
        self.__cache = {}

    def __register_data_with_tree(self) -> None:
        """Makes the leaf data consistent with the leaves in the tree.

        Removes any leaves from the character matrix, cell metadata, and
        dissimilarity maps that do not appear in the tree.
        Additionally, adds any leaves that appear in the tree but not in the
        character matrix, cell metadata, or dissimilarity map with default values.
        The default values for each table is as follows:
        * character matrix: all states are missing values (``missing_state_indicator``)
        * cell metadata: None
        * dissimilarity map: ``np.inf`` distance from the leaf to all other leaves
        """
        leaves_set = set(self.leaves)
        if self.__current_character_matrix is not None:
            remove_from_charater_matrix = set(self.__current_character_matrix.index) - leaves_set
            self.__current_character_matrix = self.__current_character_matrix.drop(index = remove_from_charater_matrix)

            add_to_character_matrix = leaves_set - set(self.__current_character_matrix.index)
            for to_add in add_to_character_matrix:
                # Note that we initialize state each iteration so that each state
                # is a new object.
                state = [self.missing_state_indicator] * self.n_character
                self.__set_character_states(to_add, state)
                self.__current_character_matrix.loc[to_add] = state

        if self.cell_meta is not None:
            remove_from_cell_meta = set(self.cell_meta.index) - leaves_set
            self.cell_meta = self.cell_meta.drop(index = remove_from_cell_meta)

            add_to_cell_meta = leaves_set - set(self.cell_meta.index)
            for to_add in add_to_cell_meta:
                self.cell_meta.loc[to_add] = None

        if self.__dissimilarity_map is not None:
            remove_from_dissimilarity_map = set(self.__dissimilarity_map.index) - leaves_set
            self.__dissimilarity_map = self.__dissimilarity_map.drop(index = remove_from_dissimilarity_map, columns = remove_from_dissimilarity_map)

            add_to_dissimilarity_map = leaves_set - set(self.__dissimilarity_map.index)
            for to_add in add_to_dissimilarity_map:
                self.__dissimilarity_map.loc[to_add] = np.inf
                self.__dissimilarity_map[to_add] = np.inf

    def add_leaf(self, parent: str, node: str) -> None:
        """Add a leaf to the given parent node. The parent node may NOT also be
        a leaf, as this makes bookkeeping the character, dissimilarity and
        cell meta quite complicated. Also, adding a leaf to an existing leaf is
        probably not a common use case.

        The new leaf is initialized to have time identical to the parent (i.e.
        branch length from parent is 0), with a character string of all missing states
        (if there is a character matrix), an empty cell meta (if it exists), and
        infinite dissimlarity with all other leaves. The current tree is modified
        in-place.

        The *caller* is expected to maintain any desired constraints (such as ultrametricity)
        by calling :func:`set_time` and/or :func:`set_branch_length`, as well as
        character states by calling :func:`set_character_states`. If this function
        is called on a tree with an existing dissimilarity map, it is expected the
        caller will re-run dissimilarity calculation, because the new leaf is
        initialized to have infinite dissimilarity with all other leaves.

        Args:
            parent: Parent node, to which to connect the leaf
            node: Name of the leaf to add

        Raises:
            CassiopeiaTreeError if ``parent`` does not exist or ``node``
                already exists, if ``parent`` is a leaf, or if the tree has not
                been initialized.
        """
        self.__check_network_initialized()

        # Make sure the node is not already in the tree and the parent exists
        if node in self.nodes:
            raise CassiopeiaTreeError(f"Node {node} already exists.")
        if parent not in self.nodes:
            raise CassiopeiaTreeError(f"Node {parent} does not exist.")
        if parent in self.leaves:
            raise CassiopeiaTreeError("Can not add a leaf to a leaf.")

        self.__add_edge(parent, node)
        self.set_branch_length(parent, node, 0)

        # reset cache because we've changed the tree topology
        self.__cache = {}

        # Update new leaf data with defaults
        self.__register_data_with_tree()

    def remove_leaf_and_prune_lineage(self, node: str) -> None:
        """Removes a leaf from the tree and prunes the lineage.

        Removes a leaf and all ancestors of that leaf that are no longer the
        ancestor of any leaves. In the context of a phylogeny, this prunes the
        lineage of all nodes no longer relevant to observed samples.
        Additionally, maintains consistency with the updated tree by removing
        the node from all leaf data.

        Args:
            node: The leaf node to be removed

        Raises:
            CassiopeiaTreeError if the tree is not initialized or input node is
            not a leaf
        """
        self.__check_network_initialized()

        if not self.is_leaf(node):
            raise CassiopeiaTreeError("Node is not a leaf.")

        if len(self.nodes) == 1:
            self.__remove_node(node)
        else:
            curr_parent = self.parent(node)
            self.__remove_node(node)
            while (
                len(self.children(curr_parent)) < 1
                and not self.is_root(curr_parent)
            ):
                next_parent = self.parent(curr_parent)
                self.__remove_node(curr_parent)
                curr_parent = next_parent

        # reset cache because we've changed the tree topology
        self.__cache = {}

        # Remove all removed nodes from data fields
        self.__register_data_with_tree()

    def collapse_unifurcations(self, source: Optional[int] = None) -> None:
        """Collapses unifurcations on the tree.

        Removes all internal nodes that have in degree and out degree of 1,
        connecting their parent and children nodes by branchs with lengths
        equal to the total time elapsed from parent to each child. Therefore
        preserves the times of nodes that are not removed.

        Args:
            source: The node at which to begin the tree traversal

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if source is None:
            source = self.root

        for node in list(self.depth_first_traverse_nodes(postorder = True, source = source)):
            if self.is_leaf(node):
                continue
            elif node == source:
                successors = self.children(node)
                if len(successors) == 1:
                    child = successors[0]
                    t = self.get_branch_length(node, child)
                    for grandchild in self.children(child):
                        t_ = self.get_branch_length(child, grandchild)
                        self.__add_edge(node, grandchild)
                        self.__set_branch_length(node, grandchild, t + t_)
                    self.__remove_node(child)
            else:
                successors = self.children(node)
                if len(successors) == 1:
                    child = successors[0]
                    parent = self.parent(node)
                    t = self.get_branch_length(parent, node)
                    t_ = self.get_branch_length(node, child)
                    self.__add_edge(parent, child)
                    self.__set_branch_length(parent, child, t + t_)
                    self.__remove_node(node)

        # reset cache because we've changed the tree topology
        self.__cache = {}

    def collapse_mutationless_edges(
        self,
        infer_ancestral_characters: bool,
    ) -> None:
        """Collapses mutationless edges in the tree in-place.

        Uses the internal node annotations of a tree to collapse edges with no
        mutations. The introduction of a missing data event is considered a
        mutation in this context. Either takes the existing character states on
        the tree or infers the annotations bottom-up from the samples obeying
        Camin-Sokal Parsimony. Preserves the times of nodes that are not removed
        by connecting the parent and children of removed nodes by branchs with
        lengths equal to the total time elapsed from parent to each child.

        Args:
            tree: A networkx DiGraph object representing the tree
            infer_ancestral_characters: Infer the ancestral characters states
                of the tree

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        if infer_ancestral_characters:
            self.reconstruct_ancestral_characters()

        for n in list(self.depth_first_traverse_nodes(postorder = True)):
            if self.is_leaf(n):
                continue
            for child in self.children(n):
                if not self.is_leaf(child):
                    t = self.get_branch_length(n, child)
                    if self.get_character_states(n) == self.get_character_states(child):
                        for grandchild in self.children(child):
                            t_ = self.get_branch_length(child, grandchild)
                            self.__add_edge(n, grandchild)
                            self.__set_branch_length(n, grandchild, t + t_)
                        self.__remove_node(child)

        # reset cache because we've changed the tree topology
        self.__cache = {}

    def get_dissimilarity_map(self) -> pd.DataFrame:
        """Gets the dissimilarity map."""

        if self.__dissimilarity_map is not None:
            return self.__dissimilarity_map.copy()
        else:
            return None

    def set_dissimilarity_map(self, dissimilarity_map: pd.DataFrame) -> None:
        """Sets the dissimilarity map variable in this object.

        Args:
            dissimilarity_map: Dissimilarity map relating all N x N distances
                between leaves.
        """
        character_matrix = self.__current_character_matrix
        if character_matrix is not None:

            if character_matrix.shape[0] != dissimilarity_map.shape[
                0
            ] or collections.Counter(
                character_matrix.index
            ) != collections.Counter(
                dissimilarity_map.index
            ) or collections.Counter(
                character_matrix.index
            ) != collections.Counter(
                dissimilarity_map.columns
            ):
                warnings.warn(
                    "The samples in the existing character matrix and "
                    "specified dissimilarity map do not agree.",
                    CassiopeiaTreeWarning,
                )

        self.__dissimilarity_map = dissimilarity_map.copy()

    def compute_dissimilarity_map(
        self,
        dissimilarity_function: Optional[
            Callable[
                [np.array, np.array, int, Dict[int, Dict[int, float]]], float
            ]
        ] = None,
        prior_transformation: str = "negative_log",
    ) -> None:
        """Computes a dissimilarity map.

        Given the dissimilarity function passed in, the pairwise dissimilarities
        will be computed over the samples in the character matrix. Populates
        the dissimilarity_map attribute in the object.

        If any of the leaves have ambiguous character states (detected by checking
        if any of the states are lists), then
        :func:`cassiopeia.solver.dissimilarity_functions.cluster_dissimilarity`
        is called instead, with the provided ``dissimilarity_function`` as
        the first argument.

        Args:
            dissimilarity_function: A function that will take in two character
                vectors and priors and produce a dissimilarity.
            prior_transformation: A function defining a transformation on the
                priors in forming weights. Supports the following
                transformations:
                    "negative_log": Transforms each probability by the negative
                        log
                    "inverse": Transforms each probability p by taking 1/p
                    "square_root_inverse": Transforms each probability by the
                        the square root of 1/p
        """

        if self.__current_character_matrix is None:
            raise CassiopeiaTreeError(
                "No character matrix is detected in this tree."
            )

        character_matrix = self.get_current_character_matrix()
        # Check if any of the leaves have ambiguous characters.
        numbaize = True
        if any(isinstance(c, tuple) for c in character_matrix.values.flatten()):
            numbaize = False
            warnings.warn(
                "Character matrix contains ambiguous characters.",
                CassiopeiaTreeWarning
            )

        weights = None
        if self.priors:
            weights = solver_utilities.transform_priors(
                self.priors, prior_transformation
            )

        N = character_matrix.shape[0]
        dissimilarity_map = utilities.compute_dissimilarity_map(
            character_matrix.to_numpy(),
            N,
            dissimilarity_function,
            weights,
            self.missing_state_indicator,
            numbaize=numbaize
        )

        dissimilarity_map = scipy.spatial.distance.squareform(dissimilarity_map)

        dissimilarity_map = pd.DataFrame(
            dissimilarity_map,
            index=character_matrix.index,
            columns=character_matrix.index,
        )

        self.set_dissimilarity_map(dissimilarity_map)

    def set_attribute(self, node: str, attribute_name: str, value: Any) -> None:
        """Sets an attribute in the tree.

        Args:
            node: Node name
            attribute_name: Name for the new attribute
            value: Value for the attribute.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        self.__network.nodes[node][attribute_name] = value

    def get_attribute(self, node: str, attribute_name: str) -> Any:
        """Retrieves the value of an attribute for a node.

        Args:
            node: Node name
            attribute_name: Name of the attribute.

        Returns:
            The value of the attribute for that node.
        Raises:
            CassiopeiaTreeError if the attribute has not been set for this node.
        """
        self.__check_network_initialized()

        try:
            return self.__network.nodes[node][attribute_name]
        except KeyError:
            raise CassiopeiaTreeError(f"Attribute {attribute_name} not "
                                    "detected for this node.")

    def filter_nodes(self, condition: Callable[[str], bool]) -> List[str]:

        self.__check_network_initialized()

        _filter = []
        for n in self.depth_first_traverse_nodes():
            if condition(n):
                _filter.append(n)

        return _filter

    def find_lcas_of_pairs(self, pairs: Optional[Union[Iterator[Tuple[str, str]], List[Tuple[str, str]]]] = None) -> Iterator[Tuple[Tuple[str, str], str]]:
        """Finds LCAs of all (provided) pairs.

        Args:
            pairs: Pairs of nodes for which to find LCAs. If not provided, LCAs of
                all pairs are computed. Defaults to None.

        Returns:
            A generator of ((u, v), LCA) tuples.

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()
        return nx.tree_all_pairs_lowest_common_ancestor(self.__network, root=self.root, pairs=pairs)

    def find_lca(self, *nodes: str) -> str:
        """Finds the LCA of all provided nodes. Internally, this function iteratively
        calls :func:`find_lcas_of_pairs`.

        Args:
            *nodes: Nodes for which to find the LCA. At least two must be provided.

        Returns:
            The LCA node

        Raises:
            CassiopeiaTreeError if less than two nodes were provided or if the
            tree has not been initialized.
        """
        self.__check_network_initialized()
        nodes = set(nodes)
        if len(nodes) < 2:
            raise CassiopeiaTreeError("At least two distinct nodes must be provided")

        while len(nodes) > 1:
            lcas = self.find_lcas_of_pairs(itertools.combinations(nodes, 2))
            nodes = set(tup[1] for tup in lcas)
        return list(nodes)[0]

    def get_distance(self, node1: str, node2: str) -> float:
        """Compute the branch distance between two nodes. This function first calls
        :func:`find_lca` to find the LCA of the two nodes, and then adds the branch
        lengths from the LCA to each node. Uses cached values if available.

        Args:
            node1: First node
            node2: Second node

        Returns:
            The branch distance between the two nodes

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "distances" not in self.__cache \
            or node1 not in self.__cache["distances"] \
            or node2 not in self.__cache["distances"][node1]:
            distances = self.__cache.setdefault("distances", {}).setdefault(node1, {})
            if node1 == node2:
                distances[node2] = 0
            else:
                lca = self.find_lca(node1, node2)
                lca_time = self.get_time(lca)
                distances[node2] = (self.get_time(node1) - lca_time) + (self.get_time(node2) - lca_time)

        return self.__cache["distances"][node1][node2]

    def get_distances(self, node: str, leaves_only: bool = False) -> Dict[str, float]:
        """Compute the branch distance between the given node and every other
        node. This function does NOT use :func:`get_distance` internally. Instead,
        it uses properties of trees to compute these much quicker, and the results
        are cached.

        Args:
            node: Node from which to compute distance to all other nodes
            leaves_only: Calculate distances to leaves only and not internal nodes.
                Defaults to False.

        Returns:
            Dictionary of distances

        Raises:
            CassiopeiaTreeError if the tree has not been initialized.
        """
        self.__check_network_initialized()

        if "distances" not in self.__cache or node not in self.__cache["distances"] or any(
            _node not in self.__cache["distances"][node] for _node in self.nodes
        ):
            distances = self.__cache.setdefault("distances", {}).setdefault(node, {})

            # First, compute distances to descendents of this node
            node_time = self.get_time(node)
            for descendant in self.depth_first_traverse_nodes(node):
                distances[descendant] = self.get_time(descendant) - node_time

            # Then, compute distances to other nodes by traversing parents one by one
            if not self.is_root(node):
                for ancestor in self.get_all_ancestors(node):
                    ancestor_time = self.get_time(ancestor)
                    ancestor_distance = node_time - ancestor_time
                    distances[ancestor] = ancestor_distance

                    for descendant in self.depth_first_traverse_nodes(ancestor, postorder=False):
                        if descendant not in distances:
                            distances[descendant] = ancestor_distance + (
                                self.get_time(descendant) - ancestor_time
                            )
        return {
            _node: distance
            for _node, distance in self.__cache["distances"][node].items()
            if self.is_leaf(_node)
        } if leaves_only else self.__cache["distances"][node]
