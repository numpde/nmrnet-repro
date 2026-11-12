#!/usr/bin/env python3
"""Report the fixture caller's ownership only for its real owned tree entries."""

from tools.container_guard import require_container

require_container()

import os
import sys
from pathlib import Path


def ownership_arguments(arguments):
    match arguments:
        case ["-c", ("%u" | "%u:%g") as output, name] | ["-c", ("%u" | "%u:%g") as output, "--", name]:
            path = Path(name).absolute()
        case _:
            return arguments
    root = Path(os.environ["NMRNET_FIXTURE_ROOT"])
    try:
        canonical = Path(os.path.realpath(path, strict=True))
        owner = path.lstat()
    except OSError:
        # The real stat owns missing, inaccessible and invalid-path diagnostics.
        return arguments
    if path != canonical or not path.is_relative_to(root):
        return arguments
    if (owner.st_uid, owner.st_gid) != (os.getuid(), os.getgid()):
        return arguments
    translated = output.replace("%u", os.environ["NMRNET_FIXTURE_UID"]).replace("%g", os.environ["NMRNET_FIXTURE_GID"])
    return ["-c", translated, *arguments[2:]]


os.execv("/usr/bin/stat", ["stat", *ownership_arguments(sys.argv[1:])])
