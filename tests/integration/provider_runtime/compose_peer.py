"""Disposable signed peers for the final Compose path, never a provider entrypoint.

Reuse ProcessPeers' signed API and RuntimePeers' scripted interpretation, with
the fixed independent coordinate/unit oracle. Shift checks here cover
population and finiteness for pasted and generated cases. Reference and capacity cases reuse
their existing independent owners. Scripted interpretation is not a live model
or deployed API witness.
"""

from tools.container_guard import require_container

require_container()

import argparse
import asyncio
import runpy
import shutil
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--delivery-project", required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--case", choices=("pasted", "smiles", "references", "capacity", "successive", "recovery"), required=True)
    options = parser.parse_args()
    # Production imports no tests; this preparation belongs only to the
    # admitted qualification invocation, after help and argument refusal.
    fixture = Path("/tmp/compose-peer")
    shutil.copytree("/fixture/tests", fixture / "tests")
    shutil.copytree("/fixture/binding-tests", fixture / "tests/integration/nmrnet")
    sys.path.insert(0, str(fixture))
    runtime = runpy.run_path(str(Path(__file__).with_name("compose_peer_runtime.py")))
    asyncio.run(runtime["serve"](options.root, options.seconds, options.case, options.delivery_project))


if __name__ == "__main__":
    main()
