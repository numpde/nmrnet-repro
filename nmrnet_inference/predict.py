"""Parse coordinate requests and admit their unit against the prediction model.

The parsed request retains its supplied coordinate unit. Each selected head must
have a qualified model unit before prediction tensor composition can proceed.
"""

from tools.container_guard import require_container

require_container()

import json
import math
import re
import struct

MAX_INPUT_BYTES = 1 << 20
MAX_ATOMS = 128
MAX_GEOMETRIES = 128
REQUEST_FIELDS = ("coordinate_unit", "atoms", "coordinates")
# Qualify each selected element checkpoint independently. Evidence for one
# checkpoint must not admit the other by accident.
# The repository adopts the released H/C model input convention as angstroms.
QUALIFIED_MODEL_COORDINATE_UNITS = {"H": "angstrom", "C": "angstrom"}


class CoordinateUnitMismatch(ValueError):
    """A declared input unit differs from the qualified model unit."""


def require_model_coordinate_unit(request_unit, element):
    """Require exact agreement with the evidence-qualified element model unit.

    Raises ``RuntimeError`` if the model unit is unqualified or invalid, and
    ``ValueError`` for an invalid element, empty request unit, or mismatch.
    Units are never normalized or converted.
    """
    if element not in QUALIFIED_MODEL_COORDINATE_UNITS:
        raise ValueError("Cannot prepare NMRNet++ prediction: element must be H or C.")
    if type(request_unit) is not str or not request_unit.strip():
        raise ValueError("Cannot prepare NMRNet++ prediction: coordinate_unit must be a non-empty string.")
    model_unit = QUALIFIED_MODEL_COORDINATE_UNITS[element]
    if model_unit is None:
        raise RuntimeError(f"Cannot prepare NMRNet++ {element} prediction: model coordinate unit has not been qualified.")
    if type(model_unit) is not str or not model_unit.strip():
        raise RuntimeError(f"Cannot prepare NMRNet++ {element} prediction: qualified model coordinate unit is invalid.")
    if request_unit != model_unit:
        raise CoordinateUnitMismatch(
            f"Cannot prepare NMRNet++ {element} prediction: coordinate_unit does not match the qualified model unit."
        )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Cannot parse prediction input: a JSON object contains a duplicate key.")
        result[key] = value
    return result


def _reject_json_constant(_constant):
    raise ValueError("Cannot parse prediction input: JSON numbers must be finite.")


def _parse_json_integer(digits):
    try:
        return int(digits)
    except ValueError:
        raise ValueError("Cannot parse prediction input: integer literal exceeds the supported digit limit.") from None


def _coordinate_number(value, atom_index, axis_index):
    field = f"coordinates[{atom_index}][{axis_index}]"
    if type(value) not in (int, float):
        raise ValueError(f"Cannot parse prediction input: {field} must be a finite number, not a boolean or string.")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"Cannot parse prediction input: {field} is outside the finite numeric range.") from None
    if not math.isfinite(number):
        raise ValueError(f"Cannot parse prediction input: {field} must be finite.")
    return number


def _float32_coordinate(value, atom_index, axis_index):
    field = f"coordinates[{atom_index}][{axis_index}]"
    number = _coordinate_number(value, atom_index, axis_index)
    try:
        converted = struct.unpack("!f", struct.pack("!f", number))[0]
    except OverflowError:
        raise ValueError(f"Cannot parse prediction input: {field} overflows float32.") from None
    if not math.isfinite(converted):
        raise ValueError(f"Cannot parse prediction input: {field} is not finite as float32.")
    return converted


def decode_request_document(raw):
    """Decode bounded scientific JSON without duplicate keys before field admission."""
    if type(raw) is not bytes:
        raise TypeError("Cannot parse prediction input: request bytes must be bytes.")
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Cannot parse prediction input: request exceeds the 1 MiB byte limit.")
    parse_error = None
    try:
        request = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_int=_parse_json_integer,
        )
    except json.JSONDecodeError as error:
        parse_error = (
            f"Cannot parse prediction input: JSON syntax is invalid at line {error.lineno}, column {error.colno}: {error.msg}."
        )
    except UnicodeDecodeError as error:
        parse_error = f"Cannot parse prediction input: request is not UTF-8 encoded JSON; invalid byte at position {error.start}."
    except RecursionError:
        raise ValueError("Cannot parse prediction input: JSON nesting exceeds the supported depth.") from None

    if parse_error is not None:
        # Do not chain the decoder error: its object retains the caller's full
        # request bytes even though the safe diagnostic needs only location data.
        raise ValueError(parse_error)

    if type(request) is not dict:
        raise ValueError("Cannot parse prediction input: the top-level JSON value must be an object.")
    return request


def parse_coordinate_source(raw):
    """Admit structural JSON and finite binary64 coordinates before unit conversion.

    The source reader owns unit meaning. This shared boundary deliberately does
    not round to model precision or admit a source unit as an executable request.
    """
    request = decode_request_document(raw)
    for field in REQUEST_FIELDS:
        if field not in request:
            raise ValueError(f"Cannot parse prediction input: required field {field!r} is missing.")
    if set(request) != set(REQUEST_FIELDS):
        raise ValueError("Cannot parse prediction input: only coordinate_unit, atoms, and coordinates are allowed.")

    coordinate_unit = request["coordinate_unit"]
    if type(coordinate_unit) is not str or not coordinate_unit.strip():
        raise ValueError("Cannot parse prediction input: coordinate_unit must be a non-empty string.")

    atoms = request["atoms"]
    if type(atoms) is not list or not 1 <= len(atoms) <= MAX_ATOMS:
        raise ValueError(f"Cannot parse prediction input: atoms must contain 1 through {MAX_ATOMS} symbols.")
    for index, atom in enumerate(atoms):
        if type(atom) is not str or re.fullmatch(r"[A-Z][a-z]?", atom) is None:
            raise ValueError(f"Cannot parse prediction input: atoms[{index}] must be an element-symbol spelling.")

    coordinates = request["coordinates"]
    if type(coordinates) is not list or len(coordinates) != len(atoms):
        raise ValueError("Cannot parse prediction input: coordinates must contain one row per atom.")
    source_coordinates = []
    for atom_index, row in enumerate(coordinates):
        if type(row) is not list or len(row) != 3:
            raise ValueError(f"Cannot parse prediction input: coordinates[{atom_index}] must contain three values.")
        source_coordinates.append([_coordinate_number(value, atom_index, axis_index) for axis_index, value in enumerate(row)])

    return {"coordinate_unit": coordinate_unit, "atoms": atoms, "coordinates": source_coordinates}


def parse_request(raw):
    """Admit executable coordinates at model precision and qualified model units.

    Source conversion must already be complete. Other element symbols remain
    molecular context and do not select a prediction head.
    """
    request = parse_coordinate_source(raw)
    coordinate_unit, atoms = request["coordinate_unit"], request["atoms"]
    float32_coordinates = [
        [_float32_coordinate(value, atom_index, axis_index) for axis_index, value in enumerate(row)]
        for atom_index, row in enumerate(request["coordinates"])
    ]

    # Reject a present head here so callers can finish admission before loading its artifacts.
    for element in ("H", "C"):
        if element in atoms:
            require_model_coordinate_unit(coordinate_unit, element)

    return {
        "coordinate_unit": coordinate_unit,
        "atoms": atoms,
        "coordinates": float32_coordinates,
    }


def _predict_loaded_element(request, element, dictionary, load_selected_element):
    """Load one element's resources, predict, then release them before the next head."""
    from .model import predict_element  # noqa: PLC0415 -- Keep source admission independent of model loading.

    model, scaler = load_selected_element(element, dictionary)
    return predict_element(request, element, model, dictionary, scaler)


def _admit_request(raw, load_dictionary):
    request = parse_request(raw)
    from .model import require_dictionary_atoms  # noqa: PLC0415 -- Reject malformed bytes before scientific admission.

    dictionary = load_dictionary()
    require_dictionary_atoms(request["atoms"], dictionary)
    return request, dictionary


class PredictionSession:
    """Own one synchronous worker's ordered preparation and one execution.

    The identity check follows NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
    nmrpeak_provider/runner_session.py::generate: only the session's current
    prepared object can enter work. Handles contain no serializable authority.
    Composition uses this synchronously on one isolated worker's serial command
    loop. The worker/controller owns startup, asynchronous cancellation and
    retirement; this session grants none of those guarantees.
    """

    def __init__(self, load_dictionary, load_selected_element):
        self._load_dictionary = load_dictionary
        self._load_selected_element = load_selected_element
        self._prepared = None
        self._executed = False

    def prepare(self, raw_requests):
        """Admit all head tensors before model loading; replace any old selection.

        Invalid replacement input revokes the earlier handle. Dictionary/artifact
        and tensor failures propagate unchanged; not every rejection is user error.
        """
        if self._executed:
            raise ValueError("Cannot prepare prediction: this session already entered execution; allocate a new worker session.")
        self._prepared = None
        require_request_batch(raw_requests)
        requests = []
        for index, raw in enumerate(raw_requests, 1):
            try:
                requests.append(parse_request(raw))
            except Exception as cause:
                cause.add_note(f"Scientific batch geometry-{index} failed while parsing its coordinate request.")
                raise
        from .model import require_dictionary_atoms  # noqa: PLC0415 -- Parse the complete batch before artifact I/O.

        dictionary = self._load_dictionary()
        for index, request in enumerate(requests, 1):
            try:
                require_dictionary_atoms(request["atoms"], dictionary)
            except Exception as cause:
                cause.add_note(f"Scientific batch geometry-{index} failed dictionary admission.")
                raise
        from .model import compose_net_input  # noqa: PLC0415 -- Parsing and dictionary admission precede tensor allocation.

        geometries = []
        for index, request in enumerate(requests, 1):
            try:
                geometries.append(
                    tuple(
                        (element, compose_net_input(request, dictionary, element))
                        for element in ("H", "C")
                        if element in request["atoms"]
                    )
                )
            except Exception as cause:
                cause.add_note(f"Scientific batch geometry-{index} failed tensor admission.")
                raise
        handle = object()
        self._prepared = handle, dictionary, tuple(geometries)
        return handle

    def execute(self, handle):
        """Consume now; yield complete private geometries when the owner advances.

        The release owner brackets each advancement with observation capture and
        exhausts this iterator before returning a whole result to the wire owner.
        This method itself must not yield: handle consumption cannot be deferred.
        """
        if self._prepared is None or handle is not self._prepared[0]:
            raise ValueError("Cannot execute prediction: the handle is not this session's current prepared input.")
        _handle, dictionary, geometries = self._prepared
        self._prepared = None
        self._executed = True
        return self._execute_geometries(dictionary, geometries)

    def _execute_geometries(self, dictionary, geometries):
        for heads in geometries:
            yield _prediction_result((element, self._predict_head(element, composed, dictionary)) for element, composed in heads)

    def _predict_head(self, element, composed, dictionary):
        # Keep model/scaler references inside one call so H resources are released
        # before C loads. The complete batch's admitted tensors remain live.
        from .model import predict_composed_element  # noqa: PLC0415 -- Only admitted execution loads models.

        model, scaler = self._load_selected_element(element, dictionary)
        return predict_composed_element(composed, model, scaler)


def require_request_batch(raw_requests):
    """Bound the private collection before parsing, encoding or model admission.

    The framed channel separately bounds aggregate encoded bytes. The host's
    resource and retirement grants still bound native work; this is no capacity
    qualification for a public offering.
    """
    if type(raw_requests) is not tuple:
        raise TypeError("Scientific preparation requires an ordered tuple of exact request bytes.")
    if not 1 <= len(raw_requests) <= MAX_GEOMETRIES:
        raise ValueError(f"Scientific preparation requires 1 through {MAX_GEOMETRIES} geometries.")
    if any(type(raw) is not bytes for raw in raw_requests):
        raise TypeError("Scientific preparation requires an ordered tuple of exact request bytes.")
    if any(len(raw) > MAX_INPUT_BYTES for raw in raw_requests):
        raise ValueError("Scientific preparation exceeds the existing per-request byte limit.")


def predict_request(raw, load_dictionary, load_selected_element):
    """Admit atoms against the release dictionary before loading any model.

    ``load_dictionary()`` returns the selected ordered release dictionary.
    ``load_selected_element(element, dictionary)`` returns only that element's
    model and paired scaler. It runs only after every input atom has a dedicated
    token, and is called sequentially for present H then C heads.

    Loader and scientific-inference exceptions reach the authorized caller with
    their causal chains unchanged. They may contain private diagnostic evidence;
    a provider must project them for its recipient rather than publish raw
    tracebacks. This callable owns no
    error logging or result publication and returns only after all heads succeed.
    """
    request, dictionary = _admit_request(raw, load_dictionary)
    return _prediction_result(
        (element, _predict_loaded_element(request, element, dictionary, load_selected_element))
        for element in ("H", "C")
        if element in request["atoms"]
    )


def _prediction_result(heads):
    """Collect complete sequential head results without publishing a partial head."""
    predictions = []
    for element, element_predictions in heads:
        predictions.extend(
            {
                "atom_index": atom_index,
                "element": element,
                "shift_ppm": shift_ppm,
            }
            for atom_index, shift_ppm in element_predictions
        )
    predictions.sort(key=lambda prediction: prediction["atom_index"])
    # Ppm follows the selected release's unchanged labels and paired scaler:
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/README.md#L4
    # https://arxiv.org/html/2601.18524v1 (Table 6)
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/data/normalize_dataset.py#L31-L38
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/tasks/uninmr.py#L381-L383
    # https://github.com/numpde/fork-of-NMRNetplusplus/blob/3ba8b9c2f3d0a390d00685d238618345c6ab24d7/uninmr/losses/atom_regloss.py#L75-L76
    # The exact selected release/checkpoint/scaler identities and round trip are recorded in note 001.
    return {"unit": "ppm", "predictions": predictions}
