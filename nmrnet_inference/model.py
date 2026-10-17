"""Compose one NMRNet++ input from admitted atoms and coordinates.

This prepares CPU tensors only; it does not qualify coordinate units or load a
model, checkpoint, scaler, or target labels. The operation order follows the
pinned upstream pipeline:
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/cropping_dataset.py#L27-L38
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/normalize_dataset.py#L20-L26
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L348-L379
The task's scalar coordinate markers retain row shape through pinned Uni-Core:
https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/data/prepend_token_dataset.py#L14-L25
https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/data/append_token_dataset.py#L14-L24
"""

from tools.container_guard import require_container

require_container()


def compose_net_input(atoms, coordinates, dictionary, element):
    """Return one padded CPU batch using the pinned NMRNet++ tensor recipe."""
    from .predict import MAX_ATOMS

    if type(atoms) is not list or not 1 <= len(atoms) <= MAX_ATOMS:
        raise ValueError(f"Cannot compose NMRNet++ input: atoms must contain 1 through {MAX_ATOMS} symbols.")
    if element not in ("H", "C"):
        raise ValueError("Cannot compose NMRNet++ input: element must be H or C.")

    token_ids = []
    for atom_index, atom in enumerate(atoms):
        if type(atom) is not str:
            raise ValueError("Cannot compose NMRNet++ input: every atom must be an element symbol.")
        index = dictionary.indices.get(atom)
        if index is None or dictionary.symbols[index] != atom or index == dictionary.unk():
            raise ValueError(
                f"Cannot compose NMRNet++ input: dictionary has no dedicated token for atom at input index {atom_index}."
            )
        token_ids.append(index)
    selected_index = dictionary.indices.get(element)
    if selected_index is None or dictionary.symbols[selected_index] != element or selected_index == dictionary.unk():
        raise ValueError(f"Cannot compose NMRNet++ input: dictionary has no dedicated token for {element!r}.")

    if type(coordinates) is not list or len(coordinates) != len(atoms) or any(
        type(row) is not list
        or len(row) != 3
        or any(type(value) not in (int, float) for value in row)
        for row in coordinates
    ):
        raise ValueError("Cannot compose NMRNet++ input: coordinates must contain one three-value row per atom.")

    import numpy as np
    import torch
    from scipy.spatial import distance_matrix
    from torch.nn.functional import pad

    try:
        real_coordinates = np.asarray(coordinates, dtype=np.float32)
    except OverflowError as error:
        raise ValueError(
            "Cannot compose NMRNet++ input: coordinate values exceed the finite float32 range."
        ) from error
    if not np.isfinite(real_coordinates).all():
        raise ValueError("Cannot compose NMRNet++ input: coordinates are not finite as float32.")
    centered_coordinates = real_coordinates - real_coordinates.mean(axis=0)
    if not np.isfinite(centered_coordinates).all():
        raise ValueError("Cannot compose NMRNet++ input: centered coordinates are not finite as float32.")

    logical_tokens = torch.tensor(
        [dictionary.bos(), *token_ids, dictionary.eos()], dtype=torch.long, device="cpu"
    )
    real_selection = [int(index == selected_index) for index in token_ids]
    logical_selection = torch.tensor(
        [dictionary.pad(), *real_selection, dictionary.pad()], dtype=torch.long, device="cpu"
    )

    coordinate_values = np.zeros((len(token_ids) + 2, 3), dtype=np.float32)
    coordinate_values[1:-1] = centered_coordinates
    distance_values = np.zeros((len(token_ids) + 2, len(token_ids) + 2), dtype=np.float32)
    distance_values[1:-1, 1:-1] = distance_matrix(
        centered_coordinates, centered_coordinates, p=2
    ).astype(np.float32)
    if not np.isfinite(distance_values).all():
        raise ValueError("Cannot compose NMRNet++ input: pairwise distances are not finite as float32.")

    edge_values = logical_tokens[:, None] * len(dictionary) + logical_tokens[None, :]
    padded_length = ((logical_tokens.numel() + 7) // 8) * 8
    padding = padded_length - logical_tokens.numel()
    return {
        "select_atom": pad(logical_selection, (0, padding), value=dictionary.pad()).unsqueeze(0),
        "src_tokens": pad(logical_tokens, (0, padding), value=dictionary.pad()).unsqueeze(0),
        "src_coord": pad(torch.from_numpy(coordinate_values), (0, 0, 0, padding)).unsqueeze(0),
        "src_distance": pad(torch.from_numpy(distance_values), (0, padding, 0, padding)).unsqueeze(0),
        "src_edge_type": pad(edge_values, (0, padding, 0, padding)).unsqueeze(0),
    }
