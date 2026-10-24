#!/usr/bin/env bash
# Shared host-delivery image: the same locked capture/build/admission owner as
# the controller, with no scientific modules or provider-specific runtime copy.

delivery_image_inputs=(containers/delivery/Dockerfile containers/delivery/Dockerfile.dockerignore
    containers/delivery/check_runtime.py
    requirements/provider-core-build.lock requirements/provider-core.lock
    packages/provider-core/pyproject.toml tools/artifacts.py tools/container_guard.py
    tools/core_environment.py tools/core_image.sh tools/delivery_image.sh tools/wheelhouse.py
    tools/provider_proxy.py tools/bound_http_proxy.py)
for delivery_input in "$repository_root"/packages/provider-core/src/provider_core/*.py; do
    delivery_image_inputs+=("${delivery_input#"$repository_root"/}")
done
unset delivery_input

source "$repository_root/tools/core_image.sh"

capture_delivery_inputs() {
    capture_core_inputs delivery delivery_image_inputs packages/provider-core/src
}

build_delivery_image() {
    build_core_image delivery delivery
}

require_delivery_image() {
    require_core_image delivery delivery delivery_image_inputs
    delivery_image_id=$core_image_id
}

require_delivery_inputs() {
    require_core_inputs delivery delivery "$@"
}
