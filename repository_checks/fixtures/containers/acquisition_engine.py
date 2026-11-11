"""Model the shared acquisition engine protocol with an explicit live registry."""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import socket
import sys
from pathlib import Path


def reject(reason):
    sys.stderr.write(f"Acquisition engine fixture refused operation: {reason}\n")
    raise SystemExit(97)


def option(arguments, name):
    if arguments.count(name) != 1 or arguments.index(name) + 1 == len(arguments):
        reject(f"one {name} value is required")
    return arguments[arguments.index(name) + 1]


class AcquisitionEngine:
    def __init__(self, root):
        self.root = root
        self.path = root / "acquisition-containers.json"
        self.containers = json.loads(self.path.read_text())
        self.identities = json.loads(os.environ["NMRNET_FAKE_ACQ_CIDS"])

    def save(self):
        self.path.write_text(json.dumps(self.containers) + "\n")

    def create(self, arguments):
        receipt = Path(option(arguments, "--cidfile"))
        role = receipt.parent.name
        if role not in self.identities:
            reject("create names an unmodeled acquisition role")
        identifier = self.identities[role]
        if re.fullmatch("[0-9a-f]{64}", identifier) is None or identifier in self.containers:
            reject("create requires a fresh exact container identity")
        network = option(arguments, "--network")
        expected = {"gateway": "host", "relay": "none", "worker-1": "container:" + self.identities["relay"]}
        if network != expected[role]:
            reject("role network mode differs from its admitted contract")
        if option(arguments, "--user") != os.environ["NMRNET_FAKE_IDENTITY_OWNER"].replace(" ", ":"):
            reject("role user differs from the admitted mapping")
        mounts = {}
        for index, argument in enumerate(arguments):
            if argument == "--mount":
                fields = dict(part.split("=", 1) for part in arguments[index + 1].split(",") if "=" in part)
                mounts[fields["dst"]] = fields["src"]
        self.containers[identifier] = {"role": role, "network": network, "mounts": mounts, "phase": "created"}
        self.save()
        receipt.write_text(identifier + "\n")
        sys.stdout.write(f"{identifier}\n")
        return 0

    def start(self, arguments):
        identifier = arguments[-1]
        record = self.containers[identifier]
        if record["phase"] != "created":
            reject("start requires its original unstarted container")
        role = record["role"]
        if role == "gateway":
            if arguments != [identifier]:
                reject("gateway start must be detached")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(Path(record["mounts"]["/gateway"]) / "proxy.sock"))
            (Path(record["mounts"]["/ready"]) / "socket").write_text("/gateway/proxy.sock\n")
            record["phase"] = "running"
        elif role == "relay":
            if arguments != [identifier]:
                reject("relay start must be detached")
            (Path(record["mounts"]["/ready"]) / "port").write_text("12345\n")
            record["phase"] = "running"
        else:
            if arguments != ["--attach", identifier]:
                reject("worker start must be attached")
            if "NMRNET_FAKE_WORKER_STDOUT" in os.environ:
                sys.stdout.write(os.environ["NMRNET_FAKE_WORKER_STDOUT"])
                sys.stdout.flush()
            if "NMRNET_FAKE_WORKER_STDERR" in os.environ:
                sys.stderr.write(os.environ["NMRNET_FAKE_WORKER_STDERR"])
                sys.stderr.flush()
            record["phase"] = "exited"
            record["exit_status"] = int(os.environ["NMRNET_FAKE_WORKER_STATUS"])
        self.save()
        if role == "worker-1":
            return record["exit_status"]
        return 0

    def inspect(self, arguments):
        if len(arguments) != 3 or arguments[0] != "--format":
            reject("inspect requires one exact projection and identity")
        record = self.containers[arguments[2]]
        projection = arguments[1]
        if projection == "{{.HostConfig.NetworkMode}}":
            sys.stdout.write(f"{record['network']}\n")
        elif projection == "{{.HostConfig.NetworkMode}} {{.State.Running}}":
            running = "true" if record["phase"] == "running" else "false"
            sys.stdout.write(f"{record['network']} {running}\n")
        elif projection == "{{.State.Running}} {{.State.ExitCode}}" and record["phase"] == "exited":
            sys.stdout.write(f"false {record['exit_status']}\n")
        else:
            reject("inspect projection is outside the modeled lifecycle")
        return 0

    def remove(self, arguments):
        if len(arguments) != 2 or arguments[0] != "--force":
            reject("removal requires an exact forced identity")
        del self.containers[arguments[1]]
        self.save()
        return 0

    def census(self, arguments):
        if len(arguments) != 5 or arguments[:4] != ["--all", "--quiet", "--no-trunc", "--filter"]:
            reject("absence requires an exact full-ID census")
        selector = arguments[4]
        if not selector.startswith("id=") or re.fullmatch("[0-9a-f]{64}", selector[3:]) is None:
            reject("absence selector must be one complete container ID")
        if selector[3:] in self.containers:
            sys.stdout.write(selector[3:] + "\n")
        return 0

    def run(self, arguments):
        handlers = {"create": self.create, "start": self.start, "inspect": self.inspect, "rm": self.remove, "ps": self.census}
        if not arguments or arguments[0] not in handlers:
            reject("engine command is outside the acquisition protocol")
        return handlers[arguments[0]](arguments[1:])
