"""Load NMRNet++ resources from verified bytes and compose CPU model inputs.

The caller binds each immutable byte snapshot to the extraction receipt before
decoding; loaders accept no path to reopen afterward. Tensor composition follows
the pinned upstream pipeline:
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/cropping_dataset.py#L27-L38
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/normalize_dataset.py#L20-L26
https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L348-L379
The task's scalar coordinate markers retain row shape through pinned Uni-Core:
https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/data/prepend_token_dataset.py#L14-L25
https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/data/append_token_dataset.py#L14-L24
"""

from io import BytesIO, TextIOWrapper
from typing import NamedTuple

from tools.container_guard import require_container

require_container()


class ComposedNetInput(NamedTuple):
    model_inputs: dict
    selected_atom_indices: tuple[int, ...]


def load_checkpoint_from_bytes(content):
    """Load a CPU checkpoint from the exact bytes admitted by the caller.

    This matches pinned Uni-Core's checkpoint decoder while keeping its
    pathname reopen out of the prediction boundary:
    https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/checkpoint_utils.py#L244-L251
    """
    if type(content) is not bytes:
        raise TypeError("Cannot load NMRNet++ checkpoint: verified artifact content must be immutable bytes.")
    import torch  # noqa: PLC0415

    return torch.load(
        BytesIO(content), map_location=torch.device("cpu"), weights_only=False
    )


def load_scaler_from_bytes(content):
    """Load the paired scaler with the same joblib decoder used upstream.

    The upstream task uses this decoder on ``target_scaler.ss``:
    https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/utils/data_scaler.py#L32-L38
    """
    if type(content) is not bytes:
        raise TypeError("Cannot load NMRNet++ scaler: verified artifact content must be immutable bytes.")
    import joblib  # noqa: PLC0415

    return joblib.load(BytesIO(content))


def load_dictionary_from_bytes(content):
    """Load Uni-Core's ordered dictionary from the same verified text bytes.

    ``Dictionary.load`` accepts an open text stream, avoiding its pathname-based
    use in task setup:
    https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/data/dictionary.py#L93-L108
    """
    if type(content) is not bytes:
        raise TypeError("Cannot load NMRNet++ dictionary: verified artifact content must be immutable bytes.")
    from unicore.data import Dictionary  # noqa: PLC0415

    with TextIOWrapper(BytesIO(content), encoding="utf-8") as source:
        return Dictionary.load(source)


def compose_net_input(request, dictionary, element):
    """Require a parsed request and admit its unit before building prediction tensors."""
    from .predict import MAX_ATOMS, require_model_coordinate_unit

    if type(request) is not dict:
        raise TypeError("Cannot compose NMRNet++ input: request must be the parsed request object.")
    require_model_coordinate_unit(request.get("coordinate_unit"), element)
    atoms = request["atoms"]
    coordinates = request["coordinates"]

    if type(atoms) is not list or not 1 <= len(atoms) <= MAX_ATOMS:
        raise ValueError(f"Cannot compose NMRNet++ input: atoms must contain 1 through {MAX_ATOMS} symbols.")
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
    selected_atom_indices = tuple(
        atom_index for atom_index, selected in enumerate(real_selection) if selected
    )
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
    return ComposedNetInput(
        {
            "select_atom": pad(logical_selection, (0, padding), value=dictionary.pad()).unsqueeze(0),
            "src_tokens": pad(logical_tokens, (0, padding), value=dictionary.pad()).unsqueeze(0),
            "src_coord": pad(torch.from_numpy(coordinate_values), (0, 0, 0, padding)).unsqueeze(0),
            "src_distance": pad(torch.from_numpy(distance_values), (0, padding, 0, padding)).unsqueeze(0),
            "src_edge_type": pad(edge_values, (0, padding, 0, padding)).unsqueeze(0),
        },
        selected_atom_indices,
    )
