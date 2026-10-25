#!/usr/bin/env bash
# Installed-controller build/admission ports geometry_image.sh at 9c0d9b0.
# container.sh retains its original native-client and capture cleanup owners.
# Source/context labels identify committed inputs; immutable IDs identify output.

provider_image_inputs=(containers/provider/Dockerfile containers/provider/Dockerfile.dockerignore
    requirements/provider-core-build.lock requirements/provider-core.lock
    packages/provider-core/pyproject.toml
    nmrnet_inference/predict.py tools/artifacts.py tools/container_guard.py tools/provider_proxy.py tools/bound_http_proxy.py
    tools/provider_environment.py tools/core_environment.py tools/provider_image.sh tools/core_image.sh tools/wheelhouse.py tools/baseline_prepare.py tools/container_resources.py)
# Both packages currently have flat module layouts. Every controller module is
# captured, while separate scientific entrypoints never enter this image.
for provider_input in "$repository_root"/nmrnet_provider/*.py "$repository_root"/packages/provider-core/src/provider_core/*.py; do
    case "$provider_input" in
        */nmrnet_provider/worker_main.py|*/nmrnet_provider/native_child.py|*/nmrnet_provider/geometry_worker.py|*/nmrnet_provider/smiles_worker.py) continue ;;
    esac
    provider_image_inputs+=("${provider_input#"$repository_root"/}")
done
unset provider_input

source "$repository_root/tools/core_image.sh"

capture_provider_inputs() {
    capture_core_inputs controller provider_image_inputs nmrnet_provider packages/provider-core/src
}

build_provider_image() {
    build_core_image provider controller
    if "$print_command"; then return; fi
    source "$repository_root/tools/operator_guidance.sh"
    local -a acquisition_interface=()
    if [[ -n ${NMRNET_WLAN_INTERFACE_INPUT-} ]]; then acquisition_interface=("WLAN_INTERFACE=$NMRNET_WLAN_INTERFACE_INPUT"); fi
    next_actions=$(
        next_make_command 'If the model archives are not prepared yet:' weights/download ARCHIVE=weight.zip "${acquisition_interface[@]}"
        printf '%s\n' 'Starting the provider requires controller, prediction, geometry and delivery images and both prepared model archives.' \
            'Choose a deployment name. Write config/deployments/<name>/provider.toml and private deployment.toml; put credentials in secrets/deployments/<name>/.'
        next_make_command 'After configuring that deployment, install its inputs:' provider/deployment/install 'DEPLOYMENT=<name>'
        next_make_command 'Then configure shared delivery for the providers on this host:' delivery/deployment/install 'DEPLOYMENTS=<name>'
        next_make_command 'Start shared delivery:' delivery/deployment/up
        next_make_command 'Then start the provider:' provider/deployment/up 'DEPLOYMENT=<name>'
        printf '%s\n' 'Configuration: https://numpde.github.io/nmrnet-repro/provider.html#provider-startup-configuration'
    )
}

require_provider_image() {
    require_core_image provider controller provider_image_inputs
    provider_image_id=$core_image_id
}
