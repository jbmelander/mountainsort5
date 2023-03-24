from typing import List
import numpy as np
from .compute_pca_features import compute_pca_features
from isosplit5 import isosplit6


def pairwise_merge_step(*, snippets: np.ndarray, templates: np.ndarray, labels: np.array, times: np.array, detect_sign: int, unit_ids: np.array, detect_time_radius: int) -> np.array:
    # L = snippets.shape[0]
    # T = snippets.shape[1]
    # M = snippets.shape[2]
    K = templates.shape[0]

    new_times = np.array(times)
    new_labels = np.array(labels)

    if detect_sign < 0:
        A = -templates
    elif detect_sign > 0:
        A = templates
    else:
        A = np.abs(templates)
    
    merges = {}
    peak_channel_indices = [np.argmax(np.max(A[i], axis=0)) for i in range(K)]
    peak_values_on_channels = [np.max(A[i], axis=0) for i in range(K)]
    peak_time_indices_on_channels = [np.argmax(A[i], axis=0) for i in range(K)]
    for i1 in range(K):
        for i2 in range(i1 - 1, -1, -1): # merge everything into lower ID - important to go in reverse order
            offset1 = peak_time_indices_on_channels[i2][peak_channel_indices[i1]] - peak_time_indices_on_channels[i1][peak_channel_indices[i1]]
            offset2 = peak_time_indices_on_channels[i2][peak_channel_indices[i2]] - peak_time_indices_on_channels[i1][peak_channel_indices[i2]]
            if np.abs(offset1 - offset2) <= 4: # make sure the offsets are roughly consistent
                if peak_values_on_channels[i1][peak_channel_indices[i2]] > 0.5 * peak_values_on_channels[i2][peak_channel_indices[i2]]:
                    if peak_values_on_channels[i2][peak_channel_indices[i1]] > 0.5 * peak_values_on_channels[i1][peak_channel_indices[i1]]:
                        # print(f'Pairwise merge: comparing units {unit_ids[i1]} and {unit_ids[i2]} (offset: {offset1})')
                        if test_merge(offset_snippets(snippets[new_labels == unit_ids[i1]], offset=offset1), snippets[new_labels == unit_ids[i2]]):
                            print(f'Pairwise merge: ** merging {unit_ids[i1]} and {unit_ids[i2]} (offset: {offset1})')
                            merges[unit_ids[i1]] = {
                                'unit_id1': unit_ids[i1],
                                'unit_id2': unit_ids[i2],
                                'offset': offset1
                            }
    for unit_id in unit_ids[::-1]: # important to go in descending order for transitive merges to work
        if unit_id in merges:
            mm = merges[unit_id]
            unit_id1 = mm['unit_id1']
            unit_id2 = mm['unit_id2']
            assert unit_id == unit_id1
            offset = mm['offset']
            inds1 = np.nonzero(new_labels == unit_id1)[0]
            inds2 = np.nonzero(new_labels == unit_id2)[0]
            print(f'Performing merge of units {unit_id1} ({len(inds1)} events) and {unit_id2} ({len(inds2)} events) (offset: {offset})')
            new_times[inds1] = new_times[inds1] + offset
            new_labels[inds1] = unit_id2
    
    # need to re-sort the times after the merges (because of the time offsets)
    sort_inds = np.argsort(new_times)
    new_times = new_times[sort_inds]
    new_labels = new_labels[sort_inds]
    
    # After merges, we may now have duplicate events - let's remove them
    for unit_id in unit_ids:
        inds0 = np.nonzero(new_labels == unit_id)[0]
        if len(inds0) > 0:
            times0 = new_times[inds0]
            duplicate_inds = find_duplicate_times(times0, tol=detect_time_radius)
            if len(duplicate_inds) > 0:
                print(f'Removing {len(duplicate_inds)} duplicate events in unit {unit_id}')
                new_labels[inds0[duplicate_inds]] = 0
    
    # remove all duplicate events
    nonzero_inds = np.nonzero(new_labels)[0]
    print(f'Removing a total of {len(new_labels) - len(nonzero_inds)} duplicate events')
    new_times = new_times[nonzero_inds]
    new_labels = new_labels[nonzero_inds]

    return new_times, new_labels

def find_duplicate_times(times: np.array, *, tol: int):
    ret = []
    deleted = np.zeros((len(times),), dtype=np.int16)
    for i1 in range(len(times)):
        if not deleted[i1]:
            i2 = i1 + 1
            while i2 < len(times) and times[i2] <= times[i1] + tol:
                ret.append(i2)
                deleted[i2] = True
                i2 += 1
    return ret

def test_merge(snippets1: np.ndarray, snippets2: np.ndarray):
    L1 = snippets1.shape[0]
    L2 = snippets2.shape[0]
    T = snippets1.shape[1]
    M = snippets1.shape[2]
    V1 = snippets1.reshape((L1, T * M))
    V2 = snippets2.reshape((L2, T * M))
    Vall = np.concatenate([V1, V2], axis=0)
    features0 = compute_pca_features(Vall, npca=12) # should we hard-code this as 12?
    labels0 = isosplit6(features0.T)
    # not sure best way to test this, but for now we'll check that we only have single cluster after isosplit
    # after all, each cluster should not split on its own (branch cluster method)
    return np.max(labels0) == 1
    # dominant_label1 = np.argmax(np.bincount(labels0[:L1]))
    # dominant_label2 = np.argmax(np.bincount(labels0[L1:]))
    # return dominant_label1 == dominant_label2

def offset_snippets(snippets: np.ndarray, *, offset: int):
    return np.roll(snippets, shift=offset, axis=1)