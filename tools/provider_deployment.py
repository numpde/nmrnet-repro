"""Freeze a public deployment definition with its separately protected inputs.

Layout and bundle ownership follow magnet-deploy 0619d9ffd92a4326758b442c532aa09496161098,
scripts/provider-deployment.sh::stage_deployment_input_bundle. NMRNet's existing
startup owner accepts one private source root. Rebase only declared input paths
and the machine-owned Attempt store into that runtime configuration; preserve
the reviewed public definition and admit the result through existing owners.
"""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path

import tomllib

from nmrnet_provider.config import ConfigurationRejected, _read_provider_config, read_startup_file
from nmrnet_provider.deployment import _disjoint, admit_container, compose_bytes, inventory_containers, render_compose
from nmrnet_provider.launch_capture import capture_execution
from nmrnet_provider.process import _declared_models
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.startup_capture import _bind_path, _source_path
from provider_core.response_json import ResponseRejected, read_document
from provider_core.chat import ChatEndpoint
from tools.artifacts import fsync_directory

INPUT_ROOT = Path("/srv/nmrnet/input")


def _write(path, raw):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def freeze_deployment(definition, settings, secrets, bundle, source_root, models,
                      *, source_names, deployment, checkout, uid, gid, prepared, **identities):
    """Freeze admitted inputs into a fresh generation for Compose normalization.

    No API/model request, store opening or worker allocation occurs. Failed
    generations retain their actual configuration, and previous generations stay intact.
    The caller receives the resulting Compose configuration directly.
    """
    definition, settings, secrets, bundle, source_root, models = map(
        _bind_path, (definition, settings, secrets, bundle, source_root, models)
    )
    if any(bundle.iterdir()):
        raise ConfigurationRejected("choose a fresh empty deployment bundle; existing generations were preserved.")
    raw = read_startup_file(definition, maximum_bytes=65536, label="public deployment definition")
    local_raw = read_startup_file(settings, maximum_bytes=65536, label="private deployment settings", secret=True)
    try:
        local = tomllib.loads(local_raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ConfigurationRejected("deployment.toml must contain valid UTF-8 TOML.") from None
    if not {"store_root", "maximum_startup_bytes", "routes"} <= set(local) or set(local) - {
        "store_root", "maximum_startup_bytes", "routes", "resolvers"
    }:
        raise ConfigurationRejected("deployment.toml requires store_root, maximum_startup_bytes and routes; only external routing accepts resolvers.")
    store = _bind_path(local["store_root"])
    maximum = local["maximum_startup_bytes"]
    if type(maximum) is not int or maximum < 1:
        raise ConfigurationRejected("deployment maximum_startup_bytes must be a positive integer.")
    (bundle / "config").mkdir(mode=0o700)
    (bundle / "secrets").mkdir(mode=0o700)
    consumed = {}

    def retain(path, **limits):
        path = _bind_path(path)
        if path == definition:
            return raw
        if not path.is_relative_to(INPUT_ROOT) or len(path.relative_to(INPUT_ROOT).parts) != 1:
            raise ConfigurationRejected(
                "deployment credential, key and CA paths must name files directly under /srv/nmrnet/input/."
            )
        actual = _source_path(secrets / path.name, secrets)
        content = read_startup_file(actual, **limits)
        if path in consumed:
            if consumed[path] != content:
                raise ConfigurationRejected("a repeated deployment input changed while freezing; preserve this failed bundle.")
        else:
            _write(bundle / "secrets" / path.name, content)
            consumed[path] = content
        return content

    admitted = _read_provider_config(definition, retain)
    if Path(admitted.lifecycle.store_root) != Path("/srv/nmrnet/attempts"):
        raise ConfigurationRejected(
            "the public deployment store_root must be /srv/nmrnet/attempts; set its host location in deployment.toml."
        )
    # Render only the path-bearing scalar lines. The parsed-document comparison
    # below prevents accidental edits to scientific policy or other settings.
    document = tomllib.loads(raw.decode("utf-8"))
    document["api"]["credential_file"] = str(bundle / "secrets" / Path(document["api"]["credential_file"]).name)
    for endpoint in document["interpreter"]["endpoints"]:
        endpoint["key_file"] = str(bundle / "secrets" / Path(endpoint["key_file"]).name)
    for table in (document["api"], document["uploads"], *document["interpreter"]["endpoints"]):
        if "ca_file" in table:
            table["ca_file"] = str(bundle / "secrets" / Path(table["ca_file"]).name)
    document["lifecycle"]["store_root"] = str(store)
    lines = []
    for source_line in raw.decode("utf-8").splitlines():
        rendered = source_line
        match = re.match(r"\s*(credential_file|key_file|ca_file|store_root)\s*=", source_line)
        if match:
            if match[1] == "store_root":
                value = str(store)
            else:
                # The public definition was admitted above; only flat input
                # names may cross this path boundary, regardless of TOML quotes.
                scalar = tomllib.loads(source_line)[match[1]]
                value = str(bundle / "secrets" / Path(scalar).name)
            rendered = match[1] + " = " + json.dumps(value)
        lines.append(rendered)
    frozen = ("\n".join(lines) + "\n").encode()
    if tomllib.loads(frozen.decode()) != document:
        raise ConfigurationRejected(
            "deployment paths must use complete single-line assignments; no runtime profile was published."
        )
    if len(frozen) + sum(map(len, consumed.values())) > maximum:
        raise ConfigurationRejected("the frozen deployment exceeds maximum_startup_bytes; no runtime profile was published.")
    path = bundle / "config/provider.toml"
    _write(path, frozen)

    def read_frozen(value, **limits):
        return read_startup_file(_source_path(value, bundle), **limits)

    config = _read_provider_config(path, read_frozen)
    if store.is_relative_to(bundle) or bundle.is_relative_to(store):
        raise ConfigurationRejected("the deployment Attempt store must remain separate from its frozen input bundle.")
    capture_execution(source_root, bundle, source_names, **identities)
    # Repository declarations arrive as individual read-only source grants.
    # Checkout write modes are unrelated to the frozen config/secret policy.
    def source_bytes(value, maximum_bytes):
        with _source_path(value, source_root).open("rb") as source:
            raw = source.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ConfigurationRejected("repository declaration exceeds its bounded source read.")
        return raw

    model_raw = source_bytes(models, 65536)
    try:
        _declared_models(read_document(model_raw, 65536))
        execution = read_document((bundle / "execution.json").read_bytes(), 65536)
    except ResponseRejected as cause:
        raise ConfigurationRejected("deployment execution/model declarations require strict JSON.", cause=cause) from None
    _write(bundle / "prediction-models.json", model_raw)
    for name in ("inference.lock", "unicore-source.json"):
        _write(bundle / name, source_bytes(source_root / "requirements" / name, 1024 * 1024))
    plan = render_compose(config, deployment=deployment, checkout=checkout, bundle=bundle,
                          uid=uid, gid=gid, prepared=prepared, execution=execution, routes=local["routes"])
    external = any(route["mode"] == "external" for route in local["routes"].values())
    if external:
        resolvers = local.get("resolvers")
        if type(resolvers) is not list or not resolvers or any(type(value) is not str for value in resolvers):
            raise ConfigurationRejected("external routes require explicit IPv4 resolvers in deployment.toml.")
        try:
            addresses = [ipaddress.IPv4Address(value) for value in resolvers]
        except ipaddress.AddressValueError:
            raise ConfigurationRejected("deployment resolvers require literal IPv4 addresses.") from None
        if len(set(addresses)) != len(addresses) or any(address.is_loopback for address in addresses):
            raise ConfigurationRejected("deployment resolvers must be distinct non-loopback IPv4 addresses reachable on the selected interface.")
        _write(bundle / "resolv.conf", "".join("nameserver " + str(address) + "\n" for address in addresses).encode())
    elif "resolvers" in local:
        raise ConfigurationRejected("resolvers are unused by local/mapped routes; remove that grant.")
    _write(bundle / "compose.json", compose_bytes(plan))
    return bundle


def finalize_deployment(bundle, normalized, output):
    """Seal only after the actual Compose CLI accepted this same source recipe.

    Normalized JSON is retained as CLI evidence. The original creation recipe
    remains the grant owner; full Engine admission later compares actual grants
    with that recipe, rather than trusting a second caller-authored manifest.
    Host selection additionally requires this helper's success and retirement.
    """
    bundle, normalized, output = map(_bind_path, (bundle, normalized, output))
    try:
        source = read_document(read_startup_file(bundle / "compose.json", maximum_bytes=1024 * 1024,
                                                 label="prepared Compose recipe", secret=True).replace(b"$$", b"$"), 1024 * 1024)
        raw = read_startup_file(normalized, maximum_bytes=1024 * 1024, label="Compose normalization evidence")
        actual = read_document(raw, 1024 * 1024)
    except ResponseRejected as cause:
        raise ConfigurationRejected("deployment finalization requires strict original/normalized Compose JSON.", cause=cause) from None
    if type(actual) is not dict or actual.get("name") != source["name"] or type(actual.get("services")) is not dict or set(actual["services"]) != set(source["services"]):
        raise ConfigurationRejected("Compose normalization changed the deployment project or fixed service set; no generation was finalized.")
    for role, planned in source["services"].items():
        service = actual["services"][role]
        if type(service) is not dict or service.get("image") != planned["image"] or "build" in service or type(service.get("labels")) is not dict or any(
            service["labels"].get(name) != value for name, value in planned["labels"].items()
        ):
            raise ConfigurationRejected("Compose normalization changed a role's immutable image or bundle ownership; no generation was finalized.")
    _write(bundle / "normalized.json", raw)
    for child in bundle.rglob("*"):
        child.chmod(0o500 if child.is_dir() else 0o400)
        if child.is_dir():
            fsync_directory(child)
    _write(bundle / "complete", b"complete\n")
    (bundle / "complete").chmod(0o400)
    bundle.chmod(0o500)
    fsync_directory(bundle)
    fsync_directory(bundle.parent)
    # Output belongs to this invocation. The wrapper grants a fresh directory;
    # tests may refresh it to exercise subsequent independent generations.
    temporary = output / "bundle-name.next"
    _write(temporary, (bundle.name + "\n").encode())
    temporary.replace(output / "bundle-name")
    fsync_directory(output)
    return bundle


def _inspection(inspection, identifiers, checkout, deployment):
    """Admit raw Engine records before exposing bounded host-control fields."""
    raw_ids = read_startup_file(identifiers, maximum_bytes=7 * 65, label="project container IDs")
    try:
        requested = raw_ids.decode("ascii").splitlines()
        records = read_document(read_startup_file(inspection, maximum_bytes=2 * 1024 * 1024,
                                                  label="project container inspection"), 2 * 1024 * 1024)
    except (UnicodeError, ResponseRejected) as cause:
        raise ConfigurationRejected("deployment inventory requires complete ASCII IDs and strict Engine JSON.", cause=cause) from None
    if raw_ids != ("".join(value + "\n" for value in requested)).encode():
        raise ConfigurationRejected("project container query did not return complete newline-terminated IDs.")
    return inventory_containers(records, requested_ids=requested, checkout=_bind_path(checkout), deployment=deployment)


def creation_plan(bundle):
    """Decode the recipe owner's literal-dollar encoding without evaluating it."""
    try:
        encoded = read_startup_file(_bind_path(Path(bundle) / "compose.json"), maximum_bytes=1024 * 1024,
                                    label="original Compose creation plan", secret=True)
        return read_document(encoded.replace(b"$$", b"$"), 1024 * 1024)
    except ResponseRejected as cause:
        raise ConfigurationRejected("original Compose plan requires strict retained JSON.", cause=cause) from None


def deployment_manifest(*, bundle, output):
    """Expose fixed host filesystem/network inputs without credential grants."""
    plan = creation_plan(bundle)
    provider, prediction = (plan["services"][role] for role in ("provider", "prediction"))
    stores = [grant["source"] for grant in provider["volumes"] if not grant["read_only"]]
    if len(stores) != 1:
        raise ConfigurationRejected("provider recipe requires exactly its one writable Attempt-store bind.")
    roots = {grant["target"]: grant["source"] for grant in prediction["volumes"]}
    fields = [plan["name"], stores[0], roots["/weight"], roots["/checkpoints"]]
    for role in ("api", "interpreter", "store"):
        service, command = plan["services"][role], plan["services"][role]["command"]
        if command.count("--mode") != 1 or command.count("--interface") != 1:
            raise ConfigurationRejected("proxy recipe must retain its one explicit mode and interface.")
        mode = command[command.index("--mode") + 1]
        interface = command[command.index("--interface") + 1]
        network = plan["networks"][service["networks"][0]]["name"] if mode == "local" else ""
        fields.extend((mode, interface, network))
    for role in ("api", "interpreter", "store", "prediction", "reader", "smiles", "provider"):
        fields.append(int(plan["services"][role]["healthcheck"]["start_period"][:-1]) + 5)
    _write(output, b"".join(str(value).encode() + b"\0" for value in fields))


def admit_network(*, bundle, role, inspection):
    """Bind a local route to an observed internal bridge and its current peers."""
    plan = creation_plan(bundle)
    if role not in ("api", "interpreter", "store") or "networks" not in plan["services"][role]:
        raise ConfigurationRejected("network admission requires one explicitly local proxy role.")
    service = plan["services"][role]
    expected = plan["networks"][service["networks"][0]]["name"]
    try:
        records = read_document(read_startup_file(inspection, maximum_bytes=2 * 1024 * 1024,
                                                 label="local network inspection"), 2 * 1024 * 1024)
    except ResponseRejected as cause:
        raise ConfigurationRejected("local network inspection requires strict Engine JSON.", cause=cause) from None
    if type(records) is not list or len(records) != 1 or type(records[0]) is not dict:
        raise ConfigurationRejected("local network inspection must identify exactly its one admitted network.")
    network = records[0]
    if network.get("Id") != expected or network.get("Driver") != "bridge" or network.get("Internal") is not True:
        raise ConfigurationRejected(f"{role} local route requires its exact admitted internal Docker bridge.")
    members = network.get("Containers")
    if type(members) is not dict:
        raise ConfigurationRejected(f"{role} local network inspection is missing its current peer inventory.")
    addresses = set()
    for member in members.values():
        if type(member) is not dict or type(member.get("IPv4Address")) is not str:
            raise ConfigurationRejected(f"{role} local peer inspection has a malformed IPv4 address.")
        if not member["IPv4Address"]:
            continue
        try:
            addresses.add(str(ipaddress.IPv4Interface(member["IPv4Address"]).ip))
        except (ipaddress.AddressValueError, ipaddress.NetmaskValueError) as cause:
            raise ConfigurationRejected(f"{role} local peer inspection has an invalid IPv4 address.", cause=cause) from None
    command = service["command"]
    selected = {command[index + 1].partition("=")[2] for index, word in enumerate(command) if word == "--address"}
    if not selected <= addresses:
        raise ConfigurationRejected(f"{role} local address grant does not identify current members of its admitted bridge; refresh deployment settings.")


def inspect_deployment(*, inspection, identifiers, checkout, deployment, output, images=None, bundles=None):
    """Publish only admitted control facts, never raw environment/configuration."""
    inventory = _inspection(inspection, identifiers, checkout, deployment)
    image_records = {}
    if images is not None:
        try:
            records = read_document(read_startup_file(images, maximum_bytes=2 * 1024 * 1024,
                                                      label="immutable image inspection"), 2 * 1024 * 1024)
        except ResponseRejected as cause:
            raise ConfigurationRejected("image inspection requires strict Engine JSON.", cause=cause) from None
        if type(records) is not list or len(records) > 7:
            raise ConfigurationRejected("deployment inventory requires at most seven actual role images across its original generations.")
        for record in records:
            if type(record) is not dict or type(record.get("Id")) is not str or record["Id"] in image_records:
                raise ConfigurationRejected("deployment image inspection contains malformed or duplicate identities.")
            image_records[record["Id"]] = record
    rows = []
    for role, record in sorted(inventory.items()):
        bundle = record["Config"]["Labels"]["io.nmrnet-repro.bundle"]
        image = record.get("Image")
        if type(image) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
            raise ConfigurationRejected("project container image inspection requires a full immutable image ID.")
        fields = [role, record["Id"], bundle, image]
        if images is not None:
            if image not in image_records:
                raise ConfigurationRejected("deployment image inspection is missing an actual role's immutable image.")
            original = _bind_path(Path(bundles) / bundle)
            metadata = original.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o500:
                raise ConfigurationRejected("original deployment bundle must remain caller-owned, sealed mode 0500 and free of symlinks.")
            if read_startup_file(original / "complete", maximum_bytes=9, label="original bundle completion", secret=True) != b"complete\n":
                raise ConfigurationRejected("original deployment bundle has no complete finalization receipt.")
            plan = creation_plan(original)
            admit_container(record, role=role, original_plan=plan, image=image_records[image])
            state = record["State"]
            if state.get("Status") not in ("created", "running", "restarting", "exited", "dead", "paused", "removing"):
                raise ConfigurationRejected("deployment container has an unknown Engine process state.")
            fields += [state["Status"], str(state["Running"]).lower(), str(state["Restarting"]).lower(),
                       str(state["Pid"]), plan["services"][role]["stop_grace_period"].removesuffix("s")]
        rows.extend(value.encode() + b"\0" for value in fields)
    _write(output, b"".join(rows))


def client_inputs(*, bundle, prediction_image_id, client_root):
    """Export nonsecret client expectations from the admitted running generation.

    Host inventory supplies the actual image pin; historical numerical receipts
    keep their original execution metadata and never supply this current fact.
    """
    plan = creation_plan(bundle)
    if plan["services"]["prediction"]["image"] != prediction_image_id:
        raise ConfigurationRejected("the running prediction image differs from its admitted bundle.")
    configuration = bundle / "config/provider.toml"
    raw = read_startup_file(configuration, maximum_bytes=65536, label="frozen provider configuration")

    def read_file(path, **limits):
        if Path(path) == configuration:
            return raw
        return read_startup_file(path, **limits)

    admitted = _read_provider_config(configuration, read_file)
    for protected in (bundle.parent.parent, Path(admitted.lifecycle.store_root)):
        _disjoint(client_root, protected)
    identity = {
        "origin": admitted.api.identity.origin, "authority": admitted.api.identity.authority,
        "profile": tomllib.loads(raw.decode())["api"]["profile"],
        "topology": admitted.api.expected_topology, "provider_ref": admitted.lifecycle.scope.provider_ref,
        "interpretation_scripted": False,
        "interpreter": {"max_turns": admitted.interpreter.max_turns, "endpoints": [
            json.loads(ChatEndpoint(endpoint.spec, transport=None, request_bytes=endpoint.request_bytes,
                                    response_bytes=endpoint.response_bytes).identity)
            for endpoint in admitted.interpreter.endpoints
        ]},
        "prediction_image_id": prediction_image_id, "bundle": bundle.name,
    }
    sys.stdout.write(json.dumps(identity, sort_keys=True) + "\n")


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Admit frozen deployment plans and actual Engine evidence offline.")
    operations = parser.add_subparsers(dest="operation", required=True)
    freeze = operations.add_parser("freeze", help="Freeze one deployment generation without starting services.")
    for name in ("definition", "settings", "secrets", "bundle", "source-root", "models", "checkout", "prepared"):
        freeze.add_argument("--" + name, type=Path, required=True)
    freeze.add_argument("--source", dest="source_names", action="append", required=True)
    freeze.add_argument("--deployment", required=True)
    for name in ("uid", "gid"):
        freeze.add_argument("--" + name, type=int, required=True)
    for name in ("image-id", "geometry-image-id", "controller-image-id", "repository-revision", "upstream-revision"):
        freeze.add_argument("--" + name, required=True)
    finalize = operations.add_parser("finalize", help="Seal a prepared generation after actual Compose normalization.")
    for name in ("bundle", "normalized", "output"):
        finalize.add_argument("--" + name, type=Path, required=True)
    manifest = operations.add_parser("manifest", help="Project fixed host inputs without reading credentials.")
    manifest.add_argument("--bundle", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    client = operations.add_parser("client-inputs", help="Export current nonsecret expectations for one public-client witness.")
    client.add_argument("--bundle", type=Path, required=True)
    client.add_argument("--prediction-image-id", required=True)
    client.add_argument("--client-root", type=Path, required=True)
    network = operations.add_parser("network", help="Admit one actual local route's bridge and peers.")
    network.add_argument("--bundle", type=Path, required=True)
    network.add_argument("--inspection", type=Path, required=True)
    network.add_argument("--role", choices=("api", "interpreter", "store"), required=True)
    for operation in ("inventory", "admit"):
        command = operations.add_parser(operation, help="Inspect bounded actual ownership/grants before host control.")
        for name in ("inspection", "identifiers", "checkout", "output"):
            command.add_argument("--" + name, type=Path, required=True)
        command.add_argument("--deployment", required=True)
        if operation == "admit":
            command.add_argument("--images", type=Path, required=True)
            command.add_argument("--bundles", type=Path, required=True)
    options = parser.parse_args(arguments)
    values = vars(options)
    operation = values.pop("operation")
    try:
        if operation == "freeze":
            freeze_deployment(**values)
        elif operation == "finalize":
            finalize_deployment(**values)
        elif operation == "manifest":
            deployment_manifest(**values)
        elif operation == "client-inputs":
            client_inputs(**values)
            return 0
        elif operation == "network":
            admit_network(**values)
        else:
            inspect_deployment(**values)
    except (ConfigurationRejected, OSError) as failure:
        # Protected Docker-stderr projection for this finite receiving boundary:
        # preserve owned causal evidence without arbitrary exception stringify.
        sys.stderr.write(json.dumps(failure_evidence(failure, secrets=())) + "\n")
        return 2
    sys.stdout.write("Deployment inputs/evidence admitted offline; no API/model request or service start occurred.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
