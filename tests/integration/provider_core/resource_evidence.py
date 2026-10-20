"""Qualify observations after, and separately from, exact native retirement."""

import json


def require_cgroup_resources(facts, *, memory_bytes):
    """Require the fixed launch ceilings and actual positive kernel usage/peak."""
    limits = {"cpu.max": "200000 100000", "memory.max": str(memory_bytes), "memory.swap.max": "0", "pids.max": "64"}
    if any(facts.get(name) != value for name, value in limits.items()):
        raise AssertionError("Observed cgroup resource limits differ from the fixed contained profile")
    peak = facts.get("memory.peak", "")
    if not isinstance(peak, str) or not peak.isascii() or not peak.isdecimal() or not 0 < int(peak) <= memory_bytes:
        raise AssertionError("Observed cgroup peak memory is absent or outside the admitted memory ceiling")
    cpu = {}
    for row in facts.get("cpu.stat", "").splitlines():
        fields = row.split()
        if len(fields) != 2 or fields[0] in cpu or not fields[1].isascii() or not fields[1].isdecimal():
            raise AssertionError("Observed cgroup CPU counters are malformed or duplicated")
        cpu[fields[0]] = int(fields[1])
    if not {"usage_usec", "user_usec", "system_usec"} <= cpu.keys() or cpu["usage_usec"] <= 0:
        raise AssertionError("Observed cgroup CPU usage is missing or nonpositive")
    return facts


def retired_worker_resources(receipt, *, role, allocation):
    """Join a completed small report to its host-confirmed role/allocation."""
    if (receipt / "result").read_bytes() != f"retired {allocation}\n".encode():
        raise AssertionError("Native resource evidence requires exact host retirement first")
    with (receipt / "resources.json").open("rb") as source:
        content = source.read(16385)
    if len(content) > 16384:
        raise AssertionError("Native resource evidence exceeds its fixed 16 KiB bound")
    report = json.loads(content)
    if (report["role"], report["allocation"], report["scope"]) != (role, allocation, "cgroup_through_final_snapshot"):
        raise AssertionError("Native resource evidence differs from the retired role/allocation or observation scope")
    require_cgroup_resources(report["cgroup"], memory_bytes=4294967296 if role == "prediction" else 536870912)
    return report
