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
import sys
from pathlib import Path

import tomllib

from nmrnet_provider.config import ConfigurationRejected, _read_provider_config, read_startup_file
from nmrnet_provider.deployment import compose_bytes, render_compose
from nmrnet_provider.launch_capture import capture_execution
from nmrnet_provider.process import _declared_models
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.startup_capture import _bind_path, _host_plan, _source_path
from provider_core.response_json import ResponseRejected, read_document
from tools.artifacts import fsync_directory

INPUT_ROOT = Path("/srv/nmrnet/input")


def _write(path, raw):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def freeze_deployment(definition, settings, secrets, bundle, output, native_capture, models,
                      *, deployment, checkout, uid, gid, prepared, **identities):
    """Admit one new generation; publish its bundle name only after success.

    No API/model request, store opening or worker allocation occurs. Failed
    generations remain private evidence, and previous generations stay intact.
    The caller owns an empty bundle and selects the returned generation only after
    this contained helper has exited and its container has retired.
    """
    definition, settings, secrets, bundle, output, native_capture, models = map(
        _bind_path, (definition, settings, secrets, bundle, output, native_capture, models)
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
    _host_plan(config, bundle, path)
    if store.is_relative_to(bundle) or bundle.is_relative_to(store):
        raise ConfigurationRejected("the deployment Attempt store must remain separate from its frozen input bundle.")
    capture_execution(native_capture, bundle, **identities)
    model_raw = read_startup_file(models, maximum_bytes=65536, label="prediction model declaration")
    try:
        _declared_models(read_document(model_raw, 65536))
        execution = read_document((bundle / "execution.json").read_bytes(), 65536)
    except ResponseRejected as cause:
        raise ConfigurationRejected("deployment execution/model declarations require strict JSON.", cause=cause) from None
    _write(bundle / "prediction-models.json", model_raw)
    for name in ("inference.lock", "unicore-source.json"):
        _write(bundle / name, read_startup_file(native_capture / "sources/requirements" / name,
                                               maximum_bytes=1024 * 1024, label="frozen inference declaration"))
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
    receipt = (
        "Public definition: " + str(definition) + "\n"
        "Public definition SHA-256: " + hashlib.sha256(raw).hexdigest() + "\n"
        "Private settings SHA-256: " + hashlib.sha256(local_raw).hexdigest() + "\n"
        "Frozen configuration SHA-256: " + config.source_sha256 + "\n"
        "Only input file locations and the machine-owned store path were rebased.\n"
        "Refresh owner: make provider/deployment/install DEPLOYMENT=" + deployment + "\n"
    )
    _write(bundle / "SOURCE.txt", receipt.encode())
    _write(bundle / "complete", b"complete\n")
    for child in bundle.rglob("*"):
        child.chmod(0o500 if child.is_dir() else 0o400)
        if child.is_dir():
            fsync_directory(child)
    bundle.chmod(0o500)
    fsync_directory(bundle)
    # Output belongs to this invocation. The wrapper grants a fresh directory;
    # tests may refresh it to exercise subsequent independent generations.
    temporary = output / "bundle-name.next"
    _write(temporary, (bundle.name + "\n").encode())
    temporary.replace(output / "bundle-name")
    fsync_directory(output)
    return bundle


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Freeze one deployment offline; no service is started.")
    for name in ("definition", "settings", "secrets", "bundle", "output", "native-capture", "models", "checkout", "prepared"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--deployment", required=True)
    for name in ("uid", "gid"):
        parser.add_argument("--" + name, type=int, required=True)
    for name in ("image-id", "geometry-image-id", "controller-image-id", "repository-revision", "upstream-revision"):
        parser.add_argument("--" + name, required=True)
    options = parser.parse_args(arguments)
    try:
        freeze_deployment(**vars(options))
    except (ConfigurationRejected, OSError) as failure:
        # Same protected Docker-stderr projection as startup_capture.main:
        # preserve owned causal evidence without arbitrary exception stringify.
        sys.stderr.write(json.dumps(failure_evidence(failure, secrets=())) + "\n")
        return 2
    sys.stdout.write("Deployment inputs admitted offline; no API/model request or service start occurred.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
