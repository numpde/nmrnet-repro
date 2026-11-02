"""Read the exact immutable image selections from a frozen provider plan."""

import json
from pathlib import Path
import re
import sys


SERVICES = ("provider", "prediction", "reader", "smiles", "interpreter", "store")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def service_image_ids(path):
    """Return the complete fixed service-to-image mapping from a saved plan."""
    try:
        services = json.loads(Path(path).read_bytes())["services"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("frozen Compose plan cannot be read as a service image mapping") from error
    if type(services) is not dict or set(services) != set(SERVICES):
        raise ValueError("frozen Compose plan must name the six maintained provider services")
    images = {}
    for role in SERVICES:
        service = services[role]
        image = service.get("image") if type(service) is dict else None
        if type(image) is not str or IMAGE_ID.fullmatch(image) is None:
            raise ValueError("frozen Compose plan contains a non-immutable service image")
        images[role] = image
    return images


def main(arguments):
    if len(arguments) != 2 or arguments[0] not in {"services", "shared-runtime"}:
        raise ValueError("usage: compose_image_ids.py services|shared-runtime COMPOSE.json")
    images = service_image_ids(arguments[1])
    if arguments[0] == "shared-runtime":
        if images["interpreter"] != images["store"]:
            raise ValueError("interpreter and store proxies do not share one runtime image")
        print(images["interpreter"])
    else:
        for role in SERVICES:
            print(f"{role}\t{images[role]}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ValueError as error:
        print(f"Cannot read frozen Compose image IDs: {error}.", file=sys.stderr)
        raise SystemExit(2) from None
