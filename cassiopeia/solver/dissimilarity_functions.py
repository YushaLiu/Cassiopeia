"""
A library that contains dissimilarity functions for the purpose of comparing
phylogenetic samples.
"""
import numba
import numpy as np
from typing import Dict, List, Optional


def weighted_hamming_distance(
    s1: List[int], s2: List[int], priors: Optional[Dict[int, Dict[int, float]]] = None, missing_state = -1,
) -> float:
    """Computes the weighted hamming distance between samples.

    Evaluates the dissimilarity of two phylogenetic samples on the basis of
    their shared indel states and the probability of these indel states
    occurring. Specifically, for a given character, if two states are identical
    we decrement the dissimilarity by the probability of these two occurring
    independently; if the two states disagree, we increment the dissimilarity by
    the probability of these states occurring. We normalize the dissimilarity
    by the number of non-missing characters shared by the two samples.

    If priors are not given, then we increment dissimilairty by +2 if the states
    are different, +1 if one state is uncut and the other is an indel, and +0 if
    the two states are identical.

    Args:
        s1: Character states of the first sample
        s2: Character states of the second sample
        priors: A dictionary storing the state priors for each character. This
            should be a nested dictionary where each key corresponds to character
            that then indexes another dictionary storing the probability of each
            observed state. (Character -> State -> Probability)

    Returns:
        A dissimilairty score.

    """
    d = 0
    num_present = 0
    for i in range(len(s1)):
        
        if s1[i] == missing_state or s2[i] == missing_state:
            continue

        num_present += 1
        
        if priors:
            if s1[i] == s2[i] and (s1[i] != 0):
                d += (2*np.log(priors[i][s1[i]]))

        if s1[i] != s2[i]:
            if s1[i] == 0 or s2[i] == 0:
                if priors:
                    if s1[i] != 0:
                        d -= np.log(priors[i][s1[i]])
                    else:
                        d -= np.log(priors[i][s2[i]])
                else:
                    d += 1
            else:
                if priors:
                    d -= (np.log(priors[i][s1[i]]) + np.log(
                        priors[i][s2[i]]
                    ))
                else:
                    d += 2

    if num_present == 0:
        return 0

    return d / num_present

@numba.jit(nopython=True)
def hamming_distance(s1: np.array(int), s2: np.array(int)) -> int:
    """Computes the vanilla hamming distance between two samples.

    Counts the number of positions that two samples disagree at.

    Args:
        s1: The first sample
        s2: The second sample

    Returns:
        The number of positions two nodes disagree at.
    """

    dist = 0

    for i in range(len(s1)):

        if s1[i] != s2[i]:
            dist += 1

    return dist
