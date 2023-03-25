from typing import Union, Tuple, List
import numpy as np
import math
import time
import spikeinterface as si
from .Scheme2SortingParameters import Scheme2SortingParameters
from .Scheme1SortingParameters import Scheme1SortingParameters
from ..core.detect_spikes import detect_spikes
from ..core.extract_snippets import extract_snippets
from .sorting_scheme1 import sorting_scheme1
from ..core.SnippetClassifier import SnippetClassifier
from ..core.pairwise_merge_step import remove_duplicate_events
from ..core.get_sampled_recording_for_training import get_sampled_recording_for_training


# This is an extension of sorting_scheme1
def sorting_scheme2(
    recording: si.BaseRecording, *,
    sorting_parameters: Scheme2SortingParameters
) -> si.BaseSorting:
    """MountainSort 5 sorting scheme 2

    Args:
        recording (si.BaseRecording): SpikeInterface recording object
        sorting_parameters (Scheme2SortingParameters): Sorting parameters

    Returns:
        si.BaseSorting: SpikeInterface sorting object
    """
    M = recording.get_num_channels()
    N = recording.get_num_frames()
    sampling_frequency = recording.sampling_frequency
    channel_locations = recording.get_channel_locations()

    sorting_parameters.check_valid(M=M, N=N, sampling_frequency=sampling_frequency, channel_locations=channel_locations)

    # Subsample the recording for training
    if sorting_parameters.training_duration_sec is not None:
        training_recording = get_sampled_recording_for_training(
            recording=recording,
            training_duration_sec=sorting_parameters.training_duration_sec,
            mode=sorting_parameters.training_recording_sampling_mode
        )
    else:
        training_recording = recording

    # Run the first phase of spike sorting (same as sorting_scheme1)
    sorting1 = sorting_scheme1(
        recording=training_recording,
        sorting_parameters=Scheme1SortingParameters(
            detect_threshold=sorting_parameters.phase1_detect_threshold,
            detect_sign=sorting_parameters.detect_sign,
            detect_time_radius_msec=sorting_parameters.phase1_detect_time_radius_msec,
            detect_channel_radius=sorting_parameters.phase1_detect_channel_radius,
            snippet_mask_radius=sorting_parameters.snippet_mask_radius,
            snippet_T1=sorting_parameters.snippet_T1,
            snippet_T2=sorting_parameters.snippet_T2,
            npca_per_branch=sorting_parameters.phase1_npca_per_branch,
            pairwise_merge_step=sorting_parameters.phase1_pairwise_merge_step
        )
    )
    times, labels = get_times_labels_from_sorting(sorting1)
    K = np.max(labels)

    print('Loading training traces')
    # Load the traces from the training recording
    training_traces = training_recording.get_traces()
    training_snippets = extract_snippets(
        traces=training_traces,
        channel_locations=None,
        mask_radius=None,
        times=times,
        channel_indices=None,
        T1=sorting_parameters.snippet_T1,
        T2=sorting_parameters.snippet_T2
    )

    print('Training classifier')
    # Train the classifier based on the labels obtained from the first phase sorting
    snippet_classifer = SnippetClassifier(npca=sorting_parameters.classifier_npca)
    for k in range(1, K + 1):
        inds0 = np.where(labels == k)[0]
        snippets0 = training_snippets[inds0]
        template0 = np.median(snippets0, axis=0) # T x M
        if sorting_parameters.detect_sign < 0:
            AA = -template0
        elif sorting_parameters.detect_sign > 0:
            AA = template0
        else:
            AA = np.abs(template0)
        peak_indices_over_channels = np.argmax(AA, axis=0)
        peak_values_over_channels = np.max(AA, axis=0)
        offsets_to_include = []
        for m in range(M):
            if peak_values_over_channels[m] > sorting_parameters.detect_threshold * 0.5:
                offsets_to_include.append(peak_indices_over_channels[m] - sorting_parameters.snippet_T1)
        offsets_to_include = np.unique(offsets_to_include)
        for offset in offsets_to_include:
            snippet_classifer.add_training_snippets(
                snippets=subsample_snippets(np.roll(snippets0, shift=-offset, axis=1), sorting_parameters.max_num_snippets_per_training_batch),
                label=k,
                offset=offset
            )
    training_snippets = None # Free up memory
    print('Fitting model')
    snippet_classifer.fit()

    # Now that we have the classifier, we can do the full sorting
    # Iterate over time chunks, detect and classify all spikes, and collect the results
    chunk_size = int(math.ceil(100e6 / recording.get_num_channels())) # size of chunks in samples
    print(f'Chunk size: {chunk_size / recording.sampling_frequency} sec')
    chunks = get_time_chunks(recording.get_num_samples(), chunk_size=chunk_size, padding=1000)
    times_list = []
    labels_list = []
    for i, chunk in enumerate(chunks):
        print(f'Time chunk {i + 1} of {len(chunks)}')
        print('Loading traces')
        traces_chunk = recording.get_traces(start_frame=chunk.start - chunk.padding_left, end_frame=chunk.end + chunk.padding_right)
        print('Detecting spikes')
        time_radius = int(math.ceil(sorting_parameters.detect_time_radius_msec / 1000 * sampling_frequency))
        times_chunk, channel_indices_chunk = detect_spikes(
            traces=traces_chunk,
            channel_locations=channel_locations,
            time_radius=time_radius,
            channel_radius=sorting_parameters.detect_channel_radius,
            detect_threshold=sorting_parameters.detect_threshold,
            detect_sign=sorting_parameters.detect_sign,
            margin_left=sorting_parameters.snippet_T1,
            margin_right=sorting_parameters.snippet_T2,
            verbose=False
        )

        print('Extracting snippets')
        snippets2 = extract_snippets(
            traces=traces_chunk,
            channel_locations=channel_locations,
            mask_radius=sorting_parameters.snippet_mask_radius,
            times=times_chunk,
            channel_indices=channel_indices_chunk,
            T1=sorting_parameters.snippet_T1,
            T2=sorting_parameters.snippet_T2
        )

        print('Classifying snippets')
        labels_chunk, offsets_chunk = snippet_classifer.classify_snippets(snippets2)
        times_chunk = times_chunk - offsets_chunk

        # now that we offset them we need to re-sort
        sort_inds = np.argsort(times_chunk)
        times_chunk = times_chunk[sort_inds]
        labels_chunk = labels_chunk[sort_inds]

        print('Removing duplicates')
        times_chunk, labels_chunk = remove_duplicate_events(times_chunk, labels_chunk, tol=time_radius)

        # remove events in the margins
        valid_inds = np.where((chunk.padding_left <= times_chunk) & (times_chunk < chunk.total_size - chunk.padding_right))[0]
        times_chunk = times_chunk[valid_inds]
        labels_chunk = labels_chunk[valid_inds]

        # don't forget to add the chunk start time
        times_list.append(times_chunk + chunk.start - chunk.padding_left)
        labels_list.append(labels_chunk)

    # Now concatenate the results
    times_concat = np.concatenate(times_list)
    labels_concat = np.concatenate(labels_list)

    # Now create a new sorting object from the times and labels results
    sorting2 = si.NumpySorting.from_times_labels([times_concat], [labels_concat], sampling_frequency=recording.sampling_frequency)

    return sorting2

class TimeChunk:
    def __init__(self, start: int, end: int, padding_left: int, padding_right: int):
        self.start = start
        self.end = end
        self.padding_left = padding_left
        self.padding_right = padding_right
        self.total_size = self.end - self.start + padding_left + padding_right

def get_time_chunks(num_samples: int, chunk_size: int, padding: int) -> List[TimeChunk]:
    """Get time chunks
    Inputs:
        num_samples: number of samples in the recording
        chunk_size: size of each chunk in samples
        padding: padding on each side of the chunk in samples
    Returns:
        chunks: list of TimeChunk objects
    """
    chunks = []
    start = 0
    while start < num_samples:
        end = start + chunk_size
        if end > num_samples:
            end = num_samples
        padding_left = min(padding, start)
        padding_right = min(padding, num_samples - end)
        chunks.append(TimeChunk(start=start, end=end, padding_left=padding_left, padding_right=padding_right))
        start = end
    return chunks

def subsample_snippets(snippets: np.ndarray, max_num: int) -> np.ndarray:
    """Subsample snippets
    Inputs:
        snippets: 3D array of snippets (num_snippets x T x M)
        max_num: maximum number of snippets to return
    Returns:
        snippets_out: 3D array of snippets (num_snippets x T x M)
    """
    num_snippets = snippets.shape[0]
    if num_snippets > max_num:
        inds = np.arange(0, max_num) * num_snippets // max_num
        snippets_out = snippets[inds]
    else:
        snippets_out = snippets
    return snippets_out


def get_times_labels_from_sorting(sorting: si.BaseSorting) -> Tuple[np.ndarray, np.ndarray]:
    """Get times and labels from a sorting object
    Inputs:
        sorting: a sorting object
    Returns:
        times: 1D array of spike times
        labels: 1D array of spike labels
    Example:
        times, labels = get_times_labels_from_sorting(sorting)
    """
    times_list = []
    labels_list = []
    for unit_id in sorting.get_unit_ids():
        times0 = sorting.get_unit_spike_train(unit_id=unit_id)
        labels0 = np.ones(times0.shape, dtype=np.int32) * unit_id
        times_list.append(times0)
        labels_list.append(labels0)
    times = np.concatenate(times_list)
    labels = np.concatenate(labels_list)
    inds = np.argsort(times)
    times = times[inds]
    labels = labels[inds]
    return times, labels