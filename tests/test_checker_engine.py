#!/usr/bin/env python3
"""Finite native Docker fixture for the checker launcher; never contacts an engine."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
from pathlib import Path
import sys

from tools import checker


def main():
    arguments = sys.argv[1:]
    state = Path(os.environ["CHECKER_FIXTURE_STATE"])
    command = Path(sys.argv[0]).name
    if command == "rm":
        with (state / "cleanup.jsonl").open("a") as stream:
            stream.write(json.dumps(arguments) + "\n")
        target = Path(arguments[-1])
        if os.environ.get("CHECKER_FIXTURE_CAPTURE_CLEANUP_FAIL") == "1":
            if (arguments[:2] == ["-rf", "--"] and len(arguments) == 3
                    and target.parent == state.parent / ".work/checker" and target.name.startswith(".build.")):
                raise SystemExit(13)
        if os.environ.get("CHECKER_FIXTURE_RECORD_CLEANUP_FAIL") == "1":
            if (arguments[:2] == ["-f", "--"] and len(arguments) == 3
                    and target.parent == state.parent / ".work/checker" and target.name.startswith(".image-id.")):
                raise SystemExit(14)
        os.execv("/bin/rm", ["/bin/rm", *arguments])
    if command == "mv":
        (state / "record-rename.json").write_text(json.dumps(arguments))
        if os.environ.get("CHECKER_FIXTURE_RECORD_RENAME_FAIL") == "1":
            source, destination = map(Path, arguments[-2:])
            if (arguments[:2] != ["-T", "--"] or len(arguments) != 4
                    or source.parent != state.parent / ".work/checker"
                    or not source.name.startswith(".image-id.") or destination != source.parent / "image-id"):
                raise SystemExit(97)
            raise SystemExit(9)
        os.execv("/bin/mv", ["/bin/mv", *arguments])
    if arguments[:2] != ["--host", "unix:///var/run/docker.sock"]:
        raise SystemExit(97)
    arguments = arguments[2:]
    with (state / "calls.jsonl").open("a") as stream:
        json.dump({"arguments": arguments, "ambient_engine": any(
            name in os.environ for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                                            "BUILDX_BUILDER", "BUILDX_CONFIG")),
                   "buildkit": os.environ.get("DOCKER_BUILDKIT")}, stream)
        stream.write("\n")
    if arguments[0] == "run":
        mounts = {}
        for index, argument in enumerate(arguments):
            if argument == "--mount":
                fields = dict(item.split("=", 1) for item in arguments[index + 1].split(",") if "=" in item)
                mounts[fields["dst"]] = Path(fields["src"])
        if arguments[arguments.index("--network") + 1] != "none" or arguments[arguments.index("-m") + 1] != "tools.checker":
            raise SystemExit(97)
        document = checker.declaration(mounts["/input/dependencies.json"])
        if document["base"] != arguments[arguments.index("--base") + 1]:
            raise SystemExit(97)
        checker.check(mounts["/input/dependencies.json"], mounts["/dependencies"])
        print(hashlib.sha256(mounts["/input/dependencies.json"].read_bytes()).hexdigest())
    elif arguments[0] == "build":
        context = Path(arguments[-1])
        dependencies = Path(arguments[arguments.index("--build-context") + 1].split("=", 1)[1])
        checker.check(context / "requirements/checker-dependencies.json", dependencies)
        mutation = os.environ.get("CHECKER_FIXTURE_MUTATE")
        if mutation:
            Path(mutation).write_bytes(b"later live source mutation\n")
        manifest = {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in context.rglob("*") if path.is_file()}
        (state / "build.json").write_text(json.dumps({"context": str(context), "manifest": manifest,
                                                      "dependencies": str(dependencies)}))
        label = arguments[arguments.index("--label") + 1].split("=", 1)[1]
        (state / "label").write_text(label)
        if os.environ.get("CHECKER_FIXTURE_BUILD_FAIL") == "1":
            raise SystemExit(7)
        print("sha256:" + "d" * 64)
    elif arguments[:2] == ["image", "inspect"]:
        if arguments[-1] != "sha256:" + "d" * 64:
            raise SystemExit(97)
        identifier = "sha256:" + ("e" if os.environ.get("CHECKER_FIXTURE_WRONG_ID") == "1" else "d") * 64
        print(identifier + " " + (state / "label").read_text())
    else:
        raise SystemExit(97)


if __name__ == "__main__":
    main()
