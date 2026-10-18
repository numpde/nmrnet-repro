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

from tools.container_guard import require_container

require_container()

from argparse import Namespace
from io import BytesIO, TextIOWrapper
from typing import NamedTuple

from .predict import MAX_ATOMS, require_model_coordinate_unit

# Shared with the upstream baseline; architecture defaults remain owned by
# Uni-Core's registered unimol_large configuration, not a copied encoder table.
# https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/script/infer.sh
# repo-check: allow-module-constant
MODEL_SETTINGS = {
    "arch": "unimol_large",
    "classification_head_name": "nmr_head",
    "num_classes": 1,
    "gaussian_kernel": True,
    "atom_descriptor": 0,
    "global_distance": False,
}


class ComposedNetInput(NamedTuple):
    model_inputs: dict
    selected_atom_indices: tuple[int, ...]


def require_dictionary_atoms(atoms, dictionary):
    """Reject atoms the pinned upstream tokenizer would map to UNK.

    The caller's atom order remains the model's row order; reject missing
    vocabulary entries before checkpoint/scaler deserialization rather than
    silently changing their identity.
    https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/data/tokenize_dataset.py#L13-L28
    """
    token_ids = []
    for atom_index, atom in enumerate(atoms):
        index = dictionary.indices.get(atom)
        if index is None or dictionary.symbols[index] != atom or index == dictionary.unk():
            raise ValueError(
                f"Cannot prepare NMRNet++ prediction: dictionary has no dedicated token for atom at input index {atom_index}."
            )
        token_ids.append(index)
    return tuple(token_ids)


def load_checkpoint_from_bytes(content):
    """Load a CPU checkpoint from the exact bytes admitted by the caller.

    This matches pinned Uni-Core's checkpoint decoder while keeping its
    pathname reopen out of the prediction boundary:
    https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/checkpoint_utils.py#L244-L251
    """
    if type(content) is not bytes:
        raise TypeError("Cannot load NMRNet++ checkpoint: verified artifact content must be immutable bytes.")
    import torch  # noqa: PLC0415

    return torch.load(BytesIO(content), map_location=torch.device("cpu"), weights_only=False)


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


def load_prediction_dictionary_from_bytes(content):
    """Append the task's MASK token to the verified ordered release dictionary.

    Only this dictionary operation is needed from task initialization:
    https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L174-L180
    """
    dictionary = load_dictionary_from_bytes(content)
    dictionary.add_symbol("[MASK]", is_special=True)
    return dictionary


def validate_model(model, float32):
    count = 0
    for kind, values in (("parameter", model.named_parameters()), ("buffer", model.named_buffers())):
        for name, value in values:
            if value.device.type != "cpu" or (value.is_floating_point() and value.dtype != float32):
                raise ValueError(f"Cannot qualify model: {kind} {name!r} must use CPU and float32 for floating values")
            if kind == "parameter":
                count += value.numel()
    if count == 0:
        raise ValueError("Cannot qualify model: constructed model has no parameters")
    return count


def load_prediction_model_from_bytes(content, dictionary):
    """Construct the real CPU float32 model without a task, dataset or loss.

    The upstream model factory only passes task.dictionary to this constructor;
    task.build_model then registers this same node head. Apply the registered
    architecture directly so its defaults remain authoritative, without parsing
    training/validation command arguments or manufacturing a task object.
    https://github.com/dptech-corp/Uni-Core/blob/ace6fae1c8479a9751f2bb1e1d6e4047427bc134/unicore/models/__init__.py#L30-L32
    https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/models/unimat.py#L204-L207
    https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/models/unimat.py#L583-L612
    https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L461-L469
    """
    state = load_checkpoint_from_bytes(content)
    import torch  # noqa: PLC0415 -- Model dependencies are loaded only after checkpoint byte admission.

    # Importing this package registers the pinned model and architectures.
    import uninmr  # noqa: F401, PLC0415 -- Register upstream architecture only for model construction.
    from unicore.models import (  # noqa: PLC0415 -- Read the populated registry after registration.
        ARCH_CONFIG_REGISTRY,
        ARCH_MODEL_REGISTRY,
    )

    args = Namespace(**MODEL_SETTINGS, max_seq_len=MAX_ATOMS + 2)
    ARCH_CONFIG_REGISTRY[args.arch](args)
    model = ARCH_MODEL_REGISTRY[args.arch](args, dictionary)
    model.register_node_classification_head(
        args.classification_head_name,
        num_classes=args.num_classes,
        extra_dim=args.atom_descriptor,
    )
    model.load_state_dict(state["model"], strict=True)
    validate_model(model, torch.float32)
    model.eval()
    return model


def compose_net_input(request, dictionary, element):
    """Require a parsed request and admit its unit before building prediction tensors."""
    if type(request) is not dict:
        raise TypeError("Cannot compose NMRNet++ input: request must be the parsed request object.")
    require_model_coordinate_unit(request.get("coordinate_unit"), element)
    atoms = request["atoms"]
    coordinates = request["coordinates"]

    if type(atoms) is not list or not 1 <= len(atoms) <= MAX_ATOMS:
        raise ValueError(f"Cannot compose NMRNet++ input: atoms must contain 1 through {MAX_ATOMS} symbols.")
    if any(type(atom) is not str for atom in atoms):
        raise ValueError("Cannot compose NMRNet++ input: every atom must be an element symbol.")
    token_ids = require_dictionary_atoms(atoms, dictionary)
    selected_index = dictionary.indices.get(element)
    if selected_index is None or dictionary.symbols[selected_index] != element or selected_index == dictionary.unk():
        raise ValueError(f"Cannot compose NMRNet++ input: dictionary has no dedicated token for {element!r}.")

    if (
        type(coordinates) is not list
        or len(coordinates) != len(atoms)
        or any(
            type(row) is not list or len(row) != 3 or any(type(value) not in (int, float) for value in row) for row in coordinates
        )
    ):
        raise ValueError("Cannot compose NMRNet++ input: coordinates must contain one three-value row per atom.")

    import numpy as np  # noqa: PLC0415 -- Invalid structure must fail before scientific imports.
    import torch  # noqa: PLC0415 -- Invalid structure must fail before scientific imports.
    from scipy.spatial import distance_matrix  # noqa: PLC0415 -- Invalid structure must fail before scientific imports.
    from torch.nn.functional import pad  # noqa: PLC0415 -- Invalid structure must fail before scientific imports.

    try:
        real_coordinates = np.asarray(coordinates, dtype=np.float32)
    except OverflowError as error:
        raise ValueError("Cannot compose NMRNet++ input: coordinate values exceed the finite float32 range.") from error
    if not np.isfinite(real_coordinates).all():
        raise ValueError("Cannot compose NMRNet++ input: coordinates are not finite as float32.")
    centered_coordinates = real_coordinates - real_coordinates.mean(axis=0)
    if not np.isfinite(centered_coordinates).all():
        raise ValueError("Cannot compose NMRNet++ input: centered coordinates are not finite as float32.")

    logical_tokens = torch.tensor([dictionary.bos(), *token_ids, dictionary.eos()], dtype=torch.long, device="cpu")
    real_selection = [int(index == selected_index) for index in token_ids]
    selected_atom_indices = tuple(atom_index for atom_index, selected in enumerate(real_selection) if selected)
    logical_selection = torch.tensor([dictionary.pad(), *real_selection, dictionary.pad()], dtype=torch.long, device="cpu")

    coordinate_values = np.zeros((len(token_ids) + 2, 3), dtype=np.float32)
    coordinate_values[1:-1] = centered_coordinates
    distance_values = np.zeros((len(token_ids) + 2, len(token_ids) + 2), dtype=np.float32)
    distance_values[1:-1, 1:-1] = distance_matrix(centered_coordinates, centered_coordinates, p=2).astype(np.float32)
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


def predict_element(request, element, model, dictionary, scaler):
    """Return this head's finite ppm values paired with their input atom indices."""
    # The composer checks this head's unit before it allocates prediction tensors.
    composed = compose_net_input(request, dictionary, element)
    if not composed.selected_atom_indices:
        return ()

    import numpy as np  # noqa: PLC0415 -- An absent nucleus returns without loading its scientific dependencies.
    import torch  # noqa: PLC0415 -- An absent nucleus returns without loading its scientific dependencies.

    model.eval()
    with torch.no_grad():
        # The pinned task registers nmr_head as a node head; UniMat selects its rows
        # with select_atom.
        # This also matches the release loss invocation.
        # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L462-L469
        # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/models/unimat.py#L276-L280
        # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L32-L35
        result = model(
            **composed.model_inputs,
            features_only=True,
            classification_head_name="nmr_head",
        )
    if not isinstance(result, tuple) or not result:
        raise ValueError("Cannot predict NMRNet++ shifts: model output must be a non-empty tuple.")
    predictions = result[0]
    expected_shape = (len(composed.selected_atom_indices), 1)
    if not isinstance(predictions, torch.Tensor):
        raise ValueError("Cannot predict NMRNet++ shifts: nmr_head must return a tensor with one scalar row per selected atom.")
    if tuple(predictions.shape) != expected_shape:
        raise ValueError(
            "Cannot predict NMRNet++ shifts: nmr_head must return one scalar row per selected atom "
            f"(expected shape {expected_shape}, received {tuple(predictions.shape)})."
        )
    if predictions.device.type != "cpu" or predictions.dtype != torch.float32:
        raise ValueError("Cannot predict NMRNet++ shifts: nmr_head output must be CPU float32.")
    if not torch.isfinite(predictions).all().item():
        raise ValueError("Cannot predict NMRNet++ shifts: nmr_head output contains non-finite values.")

    # Pinned atom_regloss_mae applies the paired scaler once to CPU predictions.
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L75-L76
    ppm = np.asarray(scaler.inverse_transform(predictions.detach().cpu()), dtype=np.float32)
    if ppm.shape != expected_shape:
        raise ValueError("Cannot predict NMRNet++ shifts: inverse scaler changed the prediction row shape.")
    if not np.isfinite(ppm).all():
        raise ValueError("Cannot predict NMRNet++ shifts: inverse-scaled ppm values are not finite.")
    return tuple((atom_index, float(value)) for atom_index, value in zip(composed.selected_atom_indices, ppm[:, 0], strict=True))
