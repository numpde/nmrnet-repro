"""Read supplied V2000 MOL coordinates without generating or removing atoms."""

from tools.container_guard import require_container

require_container()

import io
from contextlib import redirect_stderr

from nmrnet_inference.predict import MAX_ATOMS
from nmrnet_provider.input_geometry import InputRejected, coordinate_conversion, decode_source, prepare_rows


class _NativeDiagnostic(io.TextIOBase):
    """Bound native log writes as they arrive, including warnings on success."""

    def __init__(self):
        self.content = ""
        self.truncated = False

    def write(self, text):
        remaining = 8192 - len(self.content)
        self.content += text[:remaining]
        self.truncated |= len(text) > remaining
        return len(text)

    def value(self):
        if self.truncated:
            return self.content + "\n[RDKit diagnostic truncated]\n"
        return self.content

    def add_failure_note(self, failure):
        if self.value():
            failure.add_note("RDKit messages captured before the failure:\n" + self.value())


def _atom_count(text):
    """Admit dimensions, work bounds and one record before importing native code."""
    # Match native LF framing (including CRLF), rather than treating Unicode
    # line separators as headers the C++ stream reader would not see.
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if len(lines) < 5:
        raise InputRejected("Cannot read MOL coordinates: the header, counts or structure terminator is missing.")
    # RDKit uses these CTfile byte columns and may otherwise upgrade declared 2D
    # with nonzero Z to 3D. Explicit 3D also admits planar supplied structures.
    # https://github.com/rdkit/rdkit/blob/Release_2026_09_1/Code/GraphMol/FileParsers/MolFileParser.cpp#L3392-L3417
    # https://github.com/rdkit/rdkit/blob/Release_2026_09_1/Code/GraphMol/FileParsers/MolFileParser.cpp#L2420-L2446
    if lines[1].encode("utf-8")[20:22] not in (b"3D", b"3d"):
        raise InputRejected(
            "Cannot read MOL coordinates: the header must explicitly declare 3D; a 2D drawing is not supplied geometry."
        )
    counts = lines[3].encode("utf-8")
    # V3000 is rejected before entering its native parser while OSV-2022-675
    # remains unresolved. Advisory decision: requirements/preparation.md.
    # Pinned counts/version offsets:
    # https://github.com/rdkit/rdkit/blob/Release_2026_09_1/Code/GraphMol/FileParsers/MolFileParser.cpp#L3446-L3449
    # https://github.com/rdkit/rdkit/blob/Release_2026_09_1/Code/GraphMol/FileParsers/MolFileParser.cpp#L3495-L3515
    if counts[34:39] != b"V2000":
        raise InputRejected("Cannot read MOL coordinates: only V2000 is admitted; V3000 and unknown versions are blocked.")
    try:
        atoms = int(counts[:3].decode("ascii"))
        bonds = int(counts[3:6].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise InputRejected(
            "Cannot read MOL coordinates: atom and bond counts must be ASCII integers in their V2000 columns."
        ) from None
    if not 1 <= atoms <= MAX_ATOMS or bonds < 0:
        raise InputRejected(
            f"Cannot read MOL coordinates: atom count must be 1 to {MAX_ATOMS}, and bond count cannot be negative."
        )
    terminators = [index for index, line in enumerate(lines) if line == "M  END"]
    if len(terminators) != 1 or terminators[0] < 4 + atoms + bonds:
        raise InputRejected("Cannot read MOL coordinates: the declared atom/bond rows and one M  END terminator are required.")
    if any(line.strip() for line in lines[terminators[0] + 1 :]):
        raise InputRejected(
            "Cannot read one MOL structure: additional records or trailing content require an explicit record-stream reader."
        )
    return atoms


def _read_atoms(text, count):
    from rdkit import Chem, rdBase  # noqa: PLC0415 -- Reject blocked formats before loading any native parser.

    diagnostic = _NativeDiagnostic()
    # RDKit's MOL wrapper logs parse exceptions at warning level and returns None;
    # swallowed native exceptions cannot be classified more precisely by this API.
    # CaptureErrorLog alone misses them. This single-thread, one-use worker owns
    # the process logger; return it to C++ streams rather than claiming a log stack.
    # https://github.com/rdkit/rdkit/blob/Release_2026_09_1/Code/GraphMol/Wrap/rdmolfiles.cpp#L133-L145
    # https://rdkit.org/docs/source/rdkit.rdBase.html
    rdBase.LogToPythonStderr()
    try:
        with redirect_stderr(diagnostic):
            molecule = Chem.MolFromMolBlock(text, sanitize=False, removeHs=False, strictParsing=True)
    except Exception as cause:
        # Operational failures already travel on the protected worker channel.
        # Keep preceding native messages there without changing type or str().
        diagnostic.add_failure_note(cause)
        raise
    finally:
        rdBase.LogToCppStreams()
    if molecule is None:
        failure = RuntimeError(
            "Cannot read MOL coordinates: RDKit returned no geometry. "
            "Its API cannot distinguish an input parse error from an internal native failure."
        )
        diagnostic.add_failure_note(failure)
        raise failure from None
    if molecule.GetNumAtoms() != count or molecule.GetNumConformers() != 1:
        failure = RuntimeError("Cannot read MOL coordinates: RDKit did not retain the declared atoms and exactly one geometry.")
        diagnostic.add_failure_note(failure)
        raise failure
    conformer = molecule.GetConformer()
    # V2000 atom identity is its one-based atom-row number. The pinned parser adds
    # each atom and position at that row index; no atom-map label substitutes for it.
    # https://github.com/rdkit/rdkit/blob/Release_2026_09_1/Code/GraphMol/FileParsers/MolFileParser.cpp#L1761-L1778
    rows = [
        (atom.GetIdx() + 1, atom.GetSymbol(), tuple(conformer.GetAtomPosition(atom.GetIdx()))) for atom in molecule.GetAtoms()
    ]
    return rows, diagnostic.value()


def prepare_mol(source, *, source_unit):
    """Preserve one declared 3D MOL record, converting only explicitly selected units.

    No sanitization, hydrogen removal, embedding or optimization is requested.
    RDKit still perceives stereo internally; this reader makes no stereo claim.
    Native messages stay in protected diagnostics, outside exception text/repr.
    """
    text = decode_source(source)
    coordinate_conversion(source_unit)
    count = _atom_count(text)
    rows, diagnostic = _read_atoms(text, count)
    try:
        return prepare_rows(
            rows,
            source=source,
            source_unit=source_unit,
            source_record_number=1,
            source_position_kind="atom_id",
            diagnostic=diagnostic,
        )
    except InputRejected as cause:
        cause.diagnostic = diagnostic
        raise
