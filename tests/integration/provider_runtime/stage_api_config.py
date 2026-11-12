"""Inspect interpreter inputs or stage a qualification configuration."""

from tools.container_guard import require_container

require_container()

import argparse
import json
import os
from pathlib import Path

from interpreter_fragment import interpreter_fragment


def inspect_interpreter(path, output):
    """Publish private file selectors without reading a referenced credential."""
    os.umask(0o077)
    _table, inputs = interpreter_fragment(path)
    for name in ("key_file", "ca_file"):
        with (output / (name + ".txt")).open("x") as stream:
            # silent-defaults: allow-contract -- An absent optional file is serialized as an empty line for the shell reader.
            stream.write((inputs[name] or "") + "\n")
    with (output / "interpreter-inputs.json").open("x") as stream:
        stream.write(json.dumps(inputs) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--origin")
    parser.add_argument("--topology")
    parser.add_argument("--prediction-image-id")
    parser.add_argument("--deployment", action="store_true")
    parser.add_argument("--deployment-settings", type=Path)
    parser.add_argument("--api-interface")
    parser.add_argument("--api-address")
    parser.add_argument("--interpreter-route", choices=("local", "external"))
    parser.add_argument("--interpreter-interface")
    parser.add_argument("--interpreter-network")
    parser.add_argument("--interpreter-address")
    parser.add_argument("--resolver", action="append")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--inspect-interpreter", type=Path)
    selection.add_argument("--interpreter-config", type=Path)
    args = parser.parse_args()
    if args.inspect_interpreter is not None:
        if args.origin is not None or args.topology is not None:
            parser.error("interpreter file inspection does not accept API origin or topology")
        inspect_interpreter(args.inspect_interpreter, args.root)
    else:
        if args.origin is None or args.topology is None or args.prediction_image_id is None:
            parser.error("configuration staging requires explicit --origin, --topology and --prediction-image-id")
        from materialize_api_config import stage, stage_deployment

        stage(
            args.root,
            args.origin,
            args.topology,
            inputs=Path("/input"),
            interpreter_config=args.interpreter_config,
            prediction_image_id=args.prediction_image_id,
        )
        if args.deployment:
            stage_deployment(
                args.root,
                api_interface=args.api_interface,
                api_address=args.api_address,
                interpreter_route=args.interpreter_route,
                interpreter_interface=args.interpreter_interface,
                interpreter_network=args.interpreter_network,
                interpreter_address=args.interpreter_address,
                resolvers=args.resolver,
                deployment_settings=args.deployment_settings,
            )
