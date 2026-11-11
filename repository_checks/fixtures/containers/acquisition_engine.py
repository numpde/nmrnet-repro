"""Model the shared acquisition engine protocol with an explicit live registry."""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import signal
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
        if role.startswith("worker-"):
            if "NMRNET_FAKE_ACQ_WORKER_MODES" in os.environ:
                modes = json.loads(os.environ["NMRNET_FAKE_ACQ_WORKER_MODES"])
                expected_mode = {"offline": "none", "online": "container:" + self.identities["relay"]}[modes[role]]
                if network != expected_mode:
                    reject("worker network differs from the fixture's declared mode")
            if network != "none":
                relay = self.identities["relay"]
                if (
                    network != "container:" + relay
                    or relay not in self.containers
                    or self.containers[relay]["phase"] != "running"
                ):
                    reject("online worker requires its running admitted relay")
        elif network != {"gateway": "host", "relay": "none"}[role]:
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
        if role.startswith("worker-"):
            (self.root / "worker-cid-path").write_text(str(receipt))
        sys.stdout.write(f"{identifier}\n")
        return 0

    def start(self, arguments):
        identifier = arguments[-1]
        record = self.containers[identifier]
        if record["phase"] != "created":
            reject("start requires its original unstarted container")
        role = record["role"]
        if role in {"gateway", "relay"} and os.environ.get("NMRNET_FAKE_ACQ_READINESS_ROLE") == role:
            if arguments != [identifier]:
                reject("readiness fixture requires a detached start")
            scenario = os.environ["NMRNET_FAKE_ACQ_READINESS_SCENARIO"]
            record["phase"] = "exited" if scenario == "stopped" else "running"
            self.save()
            return 0
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
            if os.environ.get("NMRNET_FAKE_HOLD_ATTACH") == "1":
                record["phase"] = "running"
                self.save()

                def retire_attach(signum, _frame):
                    (self.root / "attach-retired").write_text(str(os.getpid()))
                    raise SystemExit(128 + signum)

                signal.signal(signal.SIGTERM, retire_attach)
                (self.root / "attach-ready").write_text(str(os.getpid()))
                while True:
                    signal.pause()
            if "NMRNET_FAKE_WORKER_STDOUT" in os.environ:
                sys.stdout.write(os.environ["NMRNET_FAKE_WORKER_STDOUT"])
                sys.stdout.flush()
            if "NMRNET_FAKE_WORKER_STDERR" in os.environ:
                sys.stderr.write(os.environ["NMRNET_FAKE_WORKER_STDERR"])
                sys.stderr.flush()
            record["phase"] = "exited"
            statuses = (
                json.loads(os.environ["NMRNET_FAKE_ACQ_WORKER_STATUSES"])
                if "NMRNET_FAKE_ACQ_WORKER_STATUSES" in os.environ
                else {}
            )
            record["exit_status"] = int(statuses[role] if role in statuses else os.environ["NMRNET_FAKE_WORKER_STATUS"])
        self.save()
        if role.startswith("worker-"):
            if "NMRNET_FAKE_CORRUPT_COMPLETED_WORKER_RECEIPT" in os.environ:
                receipt = Path((self.root / "worker-cid-path").read_text())
                receipt.write_text(os.environ["NMRNET_FAKE_CORRUPT_COMPLETED_WORKER_RECEIPT"] + "\n")
                (self.root / "attach-completed").write_text(str(os.getpid()))
            return record["exit_status"]
        return 0

    def inspect(self, arguments):
        if len(arguments) != 3 or arguments[0] != "--format":
            reject("inspect requires one exact projection and identity")
        record = self.containers[arguments[2]]
        projection = arguments[1]
        if projection == "{{.State.Running}}" and os.environ.get("NMRNET_FAKE_ACQ_READINESS_ROLE") == record["role"]:
            scenario = os.environ["NMRNET_FAKE_ACQ_READINESS_SCENARIO"]
            if record["phase"] != ("exited" if scenario == "stopped" else "running"):
                reject("readiness observation disagrees with modeled state")
            if scenario == "inspect-failed":
                sys.stderr.write("private inspect cause\n")
                return 37
            states = {"stopped": "false\n", "malformed": "private malformed state\n", "exhausted": "true\n"}
            if scenario not in states:
                reject("readiness scenario is not modeled")
            sys.stdout.write(states[scenario])
        elif projection == "{{.HostConfig.NetworkMode}}":
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
        if os.environ.get("NMRNET_FAKE_ACQ_REMOVE_FAIL") == self.containers[arguments[1]]["role"]:
            return 37
        del self.containers[arguments[1]]
        self.save()
        return 0

    def logs(self, arguments):
        if len(arguments) != 3 or arguments[:2] != ["--tail", "80"]:
            reject("readiness logs require the exact bounded request")
        identifier = arguments[2]
        if (
            identifier not in self.containers
            or self.containers[identifier]["role"] != os.environ["NMRNET_FAKE_ACQ_READINESS_ROLE"]
        ):
            reject("readiness logs require the still-owned role")
        sys.stdout.write("private container stdout\n")
        sys.stderr.write("private container stderr\n")
        return int(os.environ["NMRNET_FAKE_ACQ_READINESS_LOG_STATUS"])

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
        handlers = {
            "create": self.create,
            "start": self.start,
            "inspect": self.inspect,
            "logs": self.logs,
            "rm": self.remove,
            "ps": self.census,
        }
        if not arguments or arguments[0] not in handlers:
            reject("engine command is outside the acquisition protocol")
        return handlers[arguments[0]](arguments[1:])
