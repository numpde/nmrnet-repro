"""Execute the frozen wheel selector in an already-contained engine fixture."""

from tools.container_guard import require_container

require_container()

import csv
import os
import subprocess
import sys
from pathlib import Path


def wheel_capture_mounts(arguments):
    mounts = {}
    for index, argument in enumerate(arguments):
        if argument == "--mount":
            fields = dict(field.split("=", 1) for field in next(csv.reader([arguments[index + 1]])) if "=" in field)
            mounts[fields["dst"]] = fields["src"]
    return mounts


def run_wheel_capture(arguments):
    mounts = wheel_capture_mounts(arguments)
    context = str(Path(mounts["/workspace/tools/wheelhouse.py"]).parent.parent)
    selected = arguments[arguments.index("python3") :]
    selected = [mounts.get(value, value) for value in selected]
    # The provider test environment omits pip; the checker Python owns wheel selection.
    selected[0] = sys._base_executable
    return subprocess.run(selected, cwd=context, env=os.environ | {"PYTHONPATH": context}, check=False).returncode
