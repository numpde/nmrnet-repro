"""Read fixed cgroup v2 facts through the instant of a contained observation.

The kernel owns these counters. A snapshot does not establish successful science,
process exit, or simultaneous peak memory across separate containers.
"""

from tools.container_guard import require_container

require_container()

from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")


def cgroup_snapshot():
    """Return bounded raw kernel values; unavailable evidence is an error."""
    facts = {}
    for name in ("cpu.max", "cpu.stat", "memory.max", "memory.peak", "memory.swap.max", "pids.max"):
        with (CGROUP_ROOT / name).open("rb") as source:
            value = source.read(4097)
        if len(value) > 4096:
            raise ValueError(f"Cannot observe container resources: kernel file {name} exceeds its 4096-byte evidence bound.")
        facts[name] = value.decode("ascii").strip()
    return facts
