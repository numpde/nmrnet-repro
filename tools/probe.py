"""Exercise the offline lane's effective kernel restrictions and output identity."""

from tools.container_guard import require_container

require_container()

import errno
import json
import os
import socket
import sys
from pathlib import Path


def denied_write(path: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as error:
        if error.errno != errno.EROFS:
            raise
    else:
        os.close(descriptor)
        raise AssertionError(f"write unexpectedly permitted: {path}")


def main() -> None:
    expected_uid, expected_gid = map(int, sys.argv[1:])
    assert os.getuid() == expected_uid != 0
    assert os.getgid() == expected_gid != 0
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    assert int(status["CapEff"].strip(), 16) == 0
    assert status["NoNewPrivs"].strip() == "1"
    cgroup = Path("/sys/fs/cgroup")
    assert (cgroup / "memory.max").read_text().strip() == "536870912"
    assert (cgroup / "pids.max").read_text().strip() == "64"
    quota, period = map(int, (cgroup / "cpu.max").read_text().split())
    assert quota / period == 2
    denied_write("/root-write-probe")
    # Test the mounted source itself, independent of its module launch path.
    denied_write(str(Path(__file__)))
    assert {name for _, name in socket.if_nameindex()} == {"lo"}
    with socket.socket() as connection:
        connection.settimeout(2)
        try:
            connection.connect(("192.0.2.1", 443))
        except OSError as error:
            # This kernel probe must fail on unexpected network errors.
            if error.errno != errno.ENETUNREACH:
                raise AssertionError(error) from error
        else:
            raise AssertionError("offline lane unexpectedly reached a network destination")
    Path("/output/ownership").write_text(f"{os.getuid()}:{os.getgid()}\n")
    sys.stdout.write(
        json.dumps(
            {
                "uid": os.getuid(),
                "gid": os.getgid(),
                "source_write": "denied",
                "root_write": "denied",
                "network": "denied",
                "capabilities": 0,
                "no_new_privileges": True,
                "memory_bytes": 536870912,
                "pids": 64,
                "cpus": 2,
            },
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
