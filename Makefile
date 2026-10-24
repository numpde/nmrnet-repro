override SHELL := bash
override .SHELLFLAGS := -eu -o pipefail -c
override .DEFAULT_GOAL := help

.PHONY: help packages/base-images/pull test test/unit test/repository test/probe weights/download weights/check
.PHONY: packages/base-images/proxy/download packages/base-images/python/download
.PHONY: test/unit/prediction-input test/unit/baseline-prepare test/unit/baseline-run test/unit/baseline-launcher
.PHONY: test/unit/provider test/unit/provider/pdb test/unit/provider/mol2
.PHONY: test/unit/provider/worker
test/unit/provider/worker:
	bash tools/container.sh $@
.PHONY: test/unit/provider-geometry-worker
.PHONY: test/unit/smiles-protocol test/unit/smiles-worker test/unit/smiles-qualification

test/unit/smiles-protocol:
	bash tools/container.sh $@

test/unit/smiles-worker:
	bash tools/container.sh $@

test/unit/smiles-qualification:
	bash tools/container.sh $@
.PHONY: test/unit/provider-core test/unit/provider-core/normative test/unit/provider-core/adversarial
.PHONY: test/unit/provider-core/prepared-identity test/integration/provider-core/prepared-identity
.PHONY: test/unit/provider-core/credential
.PHONY: test/unit/provider-core/verbosity test/integration/provider-core/output
.PHONY: test/unit/provider-core/hello test/integration/provider-core/hello
.PHONY: test/unit/provider-core/transport test/integration/provider-core/transport
.PHONY: test/unit/provider-core/api
.PHONY: test/unit/provider-core/admission
.PHONY: test/unit/provider-core/uploads
.PHONY: test/unit/provider-core/diagnostics
.PHONY: test/unit/provider-core/recovery
.PHONY: test/integration/provider-core/api
.PHONY: test/integration/provider-core/uploads test/integration/provider-core/smiles
.PHONY: test/integration/provider-core/pasted
.PHONY: test/integration/provider-core/interpreter
.PHONY: test/integration/provider-core/config
.PHONY: test/integration/provider-core/runtime
.PHONY: test/integration/provider-core/admission
.PHONY: test/integration/provider-core/service
.PHONY: test/integration/provider-core/recovery
.PHONY: test/integration/provider-core/interpreter-ambiguity
.PHONY: test/integration/provider-core/lifecycle
.PHONY: test/integration/provider-core/chat
.PHONY: test/contract/provider-core test/integration/provider-core
.PHONY: test/integration/provider-worker
.PHONY: test/integration/provider/prediction-recovery
test/integration/provider/prediction-recovery:
	bash tools/prediction_recovery_qualification.sh

.PHONY: test/integration/provider/logging
test/integration/provider/logging:
	bash tools/provider_logging_qualification.sh
.PHONY: test/integration/provider-scientific-worker
.PHONY: test/integration/provider-scientific-worker/smiles
.PHONY: test/integration/provider-geometry-worker
.PHONY: test/integration/provider-geometry-worker/pdb
.PHONY: test/integration/provider-geometry-worker/mol2
.PHONY: test/integration/provider-geometry-worker/uploads
.PHONY: test/integration/provider-geometry-worker/smiles
.PHONY: test/integration/provider-native-geometry-worker
.PHONY: test/integration/rdkit-runtime
.PHONY: geometry/image/build
.PHONY: provider/image/build
.PHONY: test/unit/provider/image test/integration/provider-runtime
.PHONY: test/unit/provider/proxy test/integration/provider/proxy
.PHONY: test/integration/rdkit-worker
.PHONY: test/integration/mol-reader
.PHONY: test/integration/sdf-reader
.PHONY: test/integration/native-geometry-worker
.PHONY: test/integration/smiles-generator
.PHONY: test/integration/native-smiles-worker

test/integration/native-geometry-worker:
	bash tools/container.sh $@

test/integration/smiles-generator:
	bash tools/container.sh $@

test/integration/native-smiles-worker:
	bash tools/container.sh $@

test/integration/sdf-reader:
	bash tools/container.sh $@

test/integration/mol-reader:
	bash tools/container.sh $@

test/integration/rdkit-worker:
	bash tools/container.sh $@

geometry/image/build:
	bash tools/container.sh $@

provider/image/build:
	bash tools/container.sh $@

test/unit/provider/image test/integration/provider-runtime:
	bash tools/container.sh $@

test/unit/provider/proxy test/integration/provider/proxy:
	bash tools/container.sh $@

test/integration/rdkit-runtime:
	bash tools/container.sh $@

test/integration/provider-geometry-worker:
	bash tools/worker_qualification.sh geometry-success
	bash tools/worker_qualification.sh geometry-timeout

test/integration/provider-geometry-worker/pdb:
	bash tools/worker_qualification.sh geometry-pdb

test/integration/provider-geometry-worker/mol2:
	bash tools/worker_qualification.sh geometry-mol2

test/integration/provider-geometry-worker/uploads:
	bash tools/worker_qualification.sh geometry-upload

test/integration/provider-geometry-worker/smiles:
	bash tools/worker_qualification.sh geometry-smiles
	bash tools/worker_qualification.sh geometry-smiles-timeout

test/integration/provider-native-geometry-worker:
	bash tools/worker_qualification.sh geometry-native-mol
	bash tools/worker_qualification.sh geometry-native-sdf
	bash tools/worker_qualification.sh geometry-native-rejected
	bash tools/worker_qualification.sh geometry-native-failure
	bash tools/worker_qualification.sh geometry-native-timeout

test/integration/provider-scientific-worker:
	bash tools/worker_qualification.sh scientific

test/integration/provider-scientific-worker/smiles:
	bash tools/worker_qualification.sh scientific-smiles

.PHONY: test/integration/provider-scientific-worker/pasted test/integration/provider-scientific-worker/uploads
test/integration/provider-scientific-worker/pasted:
	bash tools/worker_qualification.sh scientific-pasted

test/integration/provider-scientific-worker/uploads:
	bash tools/worker_qualification.sh scientific-upload

test/integration/provider-worker:
	bash tools/worker_qualification.sh unstarted
	bash tools/worker_qualification.sh success
	bash tools/worker_qualification.sh grant-failure
.PHONY: test/repository/provider-core test/repository/operator-inputs test/repository/documentation
.PHONY: test/unit/packages
.PHONY: packages/provider-core/lock/stage packages/provider-core/lock/apply
.PHONY: packages/provider-core/wheelhouse packages/provider-core/wheelhouse/check

packages/provider-core/lock/stage:
	bash tools/packages.sh stage provider-core

packages/provider-core/lock/apply:
	bash tools/packages.sh apply provider-core

packages/provider-core/wheelhouse:
	bash tools/wheelhouse.sh download provider-core

packages/provider-core/wheelhouse/check:
	bash tools/wheelhouse.sh check provider-core

test/unit/packages:
	bash tools/container.sh $@

test/repository/provider-core:
	bash tools/container.sh $@

test/repository/operator-inputs:
	bash tools/container.sh $@

test/repository/documentation:
	bash tools/container.sh $@

.PHONY: test/repository/native-images
test/repository/native-images:
	bash tools/container.sh $@

test/unit/provider-core:
	bash tools/container.sh $@

.PHONY: test/unit/provider-core/runner
test/unit/provider-core/runner:
	bash tools/container.sh $@

.PHONY: test/integration/provider-core/runner
test/integration/provider-core/runner:
	bash tools/container.sh $@

test/unit/provider-core/credential:
	bash tools/container.sh $@

test/unit/provider-core/verbosity:
	bash tools/container.sh $@

test/integration/provider-core/output:
	bash tools/container.sh $@

test/unit/provider-core/hello:
	bash tools/container.sh $@

test/integration/provider-core/hello:
	bash tools/container.sh $@

test/unit/provider-core/transport:
	bash tools/container.sh $@

test/unit/provider-core/api:
	bash tools/container.sh $@

test/integration/provider-core/api:
	bash tools/container.sh $@

test/integration/provider-core/transport:
	bash tools/container.sh $@

test/unit/provider-core/prepared-identity:
	bash tools/container.sh $@

test/integration/provider-core/prepared-identity:
	bash tools/container.sh $@

test/integration/provider-core/uploads:
	bash tools/container.sh $@

test/integration/provider-core/smiles:
	bash tools/container.sh $@

test/integration/provider-core/pasted:
	bash tools/container.sh $@

test/integration/provider-core/interpreter:
	bash tools/container.sh $@

test/integration/provider-core/config:
	bash tools/container.sh $@

test/integration/provider-core/runtime:
	bash tools/container.sh $@

.PHONY: test/integration/provider-core/native-factory
test/integration/provider-core/native-factory:
	bash tools/container.sh $@

.PHONY: test/integration/provider-service/uploads test/integration/provider-service/pasted test/integration/provider-service/smiles
.PHONY: test/integration/provider-service/references
.PHONY: test/integration/provider-service/formats
.PHONY: test/integration/provider-service/recovery
test/integration/provider-service/recovery:
	bash tools/provider_recovery_qualification.sh

test/integration/provider-service/formats:
	bash tools/provider_service_qualification.sh formats

.PHONY: test/integration/provider-service/capacity
test/integration/provider-service/capacity:
	bash tools/provider_service_qualification.sh capacity

test/integration/provider-service/references:
	bash tools/provider_service_qualification.sh references

test/integration/provider-service/uploads:
	bash tools/provider_service_qualification.sh uploads

test/integration/provider-service/pasted:
	bash tools/provider_service_qualification.sh pasted

test/integration/provider-service/smiles:
	bash tools/provider_service_qualification.sh smiles

test/integration/provider-core/admission:
	bash tools/container.sh $@

.PHONY: test/integration/provider-core/compose
test/integration/provider-core/compose:
	bash tools/container.sh $@

test/integration/provider-core/service:
	bash tools/container.sh $@

test/integration/provider-core/recovery:
	bash tools/container.sh $@

test/integration/provider-core/interpreter-ambiguity:
	bash tools/container.sh $@

.PHONY: test/integration/provider-core/process
test/integration/provider-core/process:
	bash tools/container.sh $@

test/unit/provider-core/admission:
	bash tools/container.sh $@

test/unit/provider-core/uploads:
	bash tools/container.sh $@

test/unit/provider-core/diagnostics:
	bash tools/container.sh $@

test/unit/provider-core/recovery:
	bash tools/container.sh $@

test/integration/provider-core/lifecycle:
	bash tools/container.sh $@

test/integration/provider-core/chat:
	bash tools/container.sh $@

test/unit/provider-core/normative:
	bash tools/container.sh $@

test/unit/provider-core/adversarial:
	bash tools/container.sh $@

test/contract/provider-core:
	bash tools/container.sh $@

test/integration/provider-core:
	bash tools/container.sh $@
.PHONY: weights/inspect weights/extract
.PHONY: packages/lock/base-images/pull packages/lock/stage packages/lock/apply
.PHONY: packages/preparation/lock/stage packages/preparation/lock/apply
.PHONY: packages/wheelhouse packages/wheelhouse/check
.PHONY: test/unit/packages/wheelhouse
test/unit/packages/wheelhouse:
	bash tools/container.sh $@
.PHONY: test/repository/wheelhouse
test/repository/wheelhouse:
	bash tools/container.sh $@
.PHONY: packages/preparation/wheelhouse packages/preparation/wheelhouse/check
.PHONY: packages/image packages/import baseline/prepare baseline/run prediction/tensor-parity
.PHONY: prediction/model-parity prediction/qualify test/unit/baseline-model
.PHONY: checks/dependencies/download checks/dependencies/check checks/image/build

# GNU Make exports command-line variables to recipes by default. Keep the public
# selector names out of recipe environments and pass their literal command-line
# text under the launcher-facing NMRNET_*_INPUT names instead.
# `value` preserves selector text for launcher validation. `override` prevents
# a caller-supplied NMRNET_*_INPUT from replacing that filtered copy.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/Makefile#L74-L78
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/check-g01-release-inputs.sh#L48-L49
unexport ARCHIVE STAGE WLAN_INTERFACE LABEL_ATOL ATOL RTOL RUN_ID INPUT
unexport WHEELHOUSE
override export NMRNET_WHEELHOUSE_INPUT := $(if $(filter command line,$(origin WHEELHOUSE)),$(value WHEELHOUSE))
override export NMRNET_WHEELHOUSE_INPUT_ORIGIN := $(origin WHEELHOUSE)
unexport CONFIG SOURCE_ROOT INSTALLATION_ROOT MAXIMUM_STARTUP_BYTES MAXIMUM_NATIVE_REQUESTS HOST_DRAIN_SECONDS
unexport RECORD_SHA256
unexport LOG_LEVEL CONTAINER_ID
unexport DEPLOYMENT
override export NMRNET_DEPLOYMENT_INPUT := $(if $(filter command line,$(origin DEPLOYMENT)),$(value DEPLOYMENT))
unexport UPLOAD_RECEIPT
override export NMRNET_UPLOAD_RECEIPT_INPUT := $(if $(filter command line,$(origin UPLOAD_RECEIPT)),$(value UPLOAD_RECEIPT))
override export NMRNET_LOG_LEVEL_INPUT := $(if $(filter command line,$(origin LOG_LEVEL)),$(value LOG_LEVEL))
override export NMRNET_CONTAINER_ID_INPUT := $(if $(filter command line,$(origin CONTAINER_ID)),$(value CONTAINER_ID))
unexport FORMAT_CASES
unexport API_ROUTE INTERPRETER_ROUTE STORE_ROUTE LOCAL_NETWORK LOCAL_INTERFACE LOCAL_PEERS EXTERNAL_INTERFACE RESOLVERS MAPPED_INTERFACE MAPPED_ADDRESSES
unexport API_ORIGIN TOPOLOGY PROJECT_REF USER_ID PROVIDER_CREDENTIAL CLIENT_CREDENTIAL_DIRECTORY CA_FILE ADDRESS INTERFACE OUTPUT API_CLIENT_IMAGE RETAINED_RESULT INTERPRETER_CONFIG
override export NMRNET_ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE))
override export NMRNET_STAGE_INPUT := $(if $(filter command line,$(origin STAGE)),$(value STAGE))
override export NMRNET_WLAN_INTERFACE_INPUT := $(if $(filter command line,$(origin WLAN_INTERFACE)),$(value WLAN_INTERFACE))
override export NMRNET_WLAN_INTERFACE_INPUT_ORIGIN := $(origin WLAN_INTERFACE)
# tools/baseline.sh consumes explicit choices; ambient tolerances cannot qualify a run.
override export NMRNET_LABEL_ATOL_INPUT := $(if $(filter command line,$(origin LABEL_ATOL)),$(value LABEL_ATOL))
override export NMRNET_ATOL_INPUT := $(if $(filter command line,$(origin ATOL)),$(value ATOL))
override export NMRNET_RTOL_INPUT := $(if $(filter command line,$(origin RTOL)),$(value RTOL))
override export NMRNET_RUN_ID_INPUT := $(if $(filter command line,$(origin RUN_ID)),$(value RUN_ID))
override export NMRNET_CONFIG_INPUT := $(if $(filter command line,$(origin CONFIG)),$(value CONFIG))
override export NMRNET_SOURCE_ROOT_INPUT := $(if $(filter command line,$(origin SOURCE_ROOT)),$(value SOURCE_ROOT))
override export NMRNET_INSTALLATION_ROOT_INPUT := $(if $(filter command line,$(origin INSTALLATION_ROOT)),$(value INSTALLATION_ROOT))
override export NMRNET_MAXIMUM_STARTUP_BYTES_INPUT := $(if $(filter command line,$(origin MAXIMUM_STARTUP_BYTES)),$(value MAXIMUM_STARTUP_BYTES))
override export NMRNET_MAXIMUM_NATIVE_REQUESTS_INPUT := $(if $(filter command line,$(origin MAXIMUM_NATIVE_REQUESTS)),$(value MAXIMUM_NATIVE_REQUESTS))
override export NMRNET_HOST_DRAIN_SECONDS_INPUT := $(if $(filter command line,$(origin HOST_DRAIN_SECONDS)),$(value HOST_DRAIN_SECONDS))
override export NMRNET_RECORD_SHA256_INPUT := $(if $(filter command line,$(origin RECORD_SHA256)),$(value RECORD_SHA256))
override export NMRNET_FORMAT_CASES_INPUT := $(if $(filter command line,$(origin FORMAT_CASES)),$(value FORMAT_CASES))
override export NMRNET_API_ROUTE_INPUT := $(if $(filter command line,$(origin API_ROUTE)),$(value API_ROUTE))
override export NMRNET_INTERPRETER_ROUTE_INPUT := $(if $(filter command line,$(origin INTERPRETER_ROUTE)),$(value INTERPRETER_ROUTE))
override export NMRNET_STORE_ROUTE_INPUT := $(if $(filter command line,$(origin STORE_ROUTE)),$(value STORE_ROUTE))
override export NMRNET_LOCAL_NETWORK_INPUT := $(if $(filter command line,$(origin LOCAL_NETWORK)),$(value LOCAL_NETWORK))
override export NMRNET_LOCAL_INTERFACE_INPUT := $(if $(filter command line,$(origin LOCAL_INTERFACE)),$(value LOCAL_INTERFACE))
override export NMRNET_LOCAL_PEERS_INPUT := $(if $(filter command line,$(origin LOCAL_PEERS)),$(value LOCAL_PEERS))
override export NMRNET_EXTERNAL_INTERFACE_INPUT := $(if $(filter command line,$(origin EXTERNAL_INTERFACE)),$(value EXTERNAL_INTERFACE))
override export NMRNET_RESOLVERS_INPUT := $(if $(filter command line,$(origin RESOLVERS)),$(value RESOLVERS))
override export NMRNET_MAPPED_INTERFACE_INPUT := $(if $(filter command line,$(origin MAPPED_INTERFACE)),$(value MAPPED_INTERFACE))
override export NMRNET_MAPPED_ADDRESSES_INPUT := $(if $(filter command line,$(origin MAPPED_ADDRESSES)),$(value MAPPED_ADDRESSES))
override export NMRNET_API_ORIGIN_INPUT := $(if $(filter command line,$(origin API_ORIGIN)),$(value API_ORIGIN))
override export NMRNET_TOPOLOGY_INPUT := $(if $(filter command line,$(origin TOPOLOGY)),$(value TOPOLOGY))
override export NMRNET_PROJECT_REF_INPUT := $(if $(filter command line,$(origin PROJECT_REF)),$(value PROJECT_REF))
override export NMRNET_USER_ID_INPUT := $(if $(filter command line,$(origin USER_ID)),$(value USER_ID))
override export NMRNET_PROVIDER_CREDENTIAL_INPUT := $(if $(filter command line,$(origin PROVIDER_CREDENTIAL)),$(value PROVIDER_CREDENTIAL))
override export NMRNET_CLIENT_CREDENTIAL_DIRECTORY_INPUT := $(if $(filter command line,$(origin CLIENT_CREDENTIAL_DIRECTORY)),$(value CLIENT_CREDENTIAL_DIRECTORY))
override export NMRNET_CA_FILE_INPUT := $(if $(filter command line,$(origin CA_FILE)),$(value CA_FILE))
override export NMRNET_ADDRESS_INPUT := $(if $(filter command line,$(origin ADDRESS)),$(value ADDRESS))
override export NMRNET_INTERFACE_INPUT := $(if $(filter command line,$(origin INTERFACE)),$(value INTERFACE))
override export NMRNET_OUTPUT_INPUT := $(if $(filter command line,$(origin OUTPUT)),$(value OUTPUT))
override export NMRNET_API_CLIENT_IMAGE_INPUT := $(if $(filter command line,$(origin API_CLIENT_IMAGE)),$(value API_CLIENT_IMAGE))
override export NMRNET_RETAINED_RESULT_INPUT := $(if $(filter command line,$(origin RETAINED_RESULT)),$(value RETAINED_RESULT))
override export NMRNET_INTERPRETER_CONFIG_INPUT := $(if $(filter command line,$(origin INTERPRETER_CONFIG)),$(value INTERPRETER_CONFIG))

help:
	@printf '%s\n' \
		'make help                              List maintained commands; also the default goal.' \
		'make test                              Run the offline unit and repository suites.' \
		'make test/unit                         Run the contained unit suite.' \
		'make test/unit/provider                Run only the contained provider-input tests.' \
		'make test/unit/provider/result         Check public shift precision and exact result/evidence admission offline.' \
		'make test/unit/provider/pdb            Check PDB atom, model and alternate-location preservation offline.' \
		'make test/unit/provider/mol2           Check supplied MOL2 atom types, coordinates and source IDs offline.' \
		'make test/unit/provider/worker-startup Check one-use native worker startup and source channels offline.' \
		'make test/unit/provider/host            Check provider host orchestration and shutdown ownership offline.' \
		'make test/unit/provider-geometry-worker Run only the contained structure-reading process tests.' \
		'make test/integration/provider-worker Qualify exact-container retirement and controller confirmation offline.' \
		'make test/integration/provider-geometry-worker Check separate structure reading and retirement of a stuck reader offline.' \
		'make test/integration/provider-geometry-worker/pdb Check PDB alternate selection in a separate contained reader offline.' \
		'make test/integration/provider-geometry-worker/mol2 Check ordered MOL2 records in a separate contained reader offline.' \
		'make test/integration/provider-geometry-worker/uploads Check verified Uploads through the isolated reader and Job lifecycle.' \
		'make test/integration/smiles-generator Check generated 3D conformers and atom mapping from SMILES offline.' \
		'make test/unit/smiles-protocol          Check exact generated bytes, provenance, shortfall and frame limits without RDKit.' \
		'make test/unit/smiles-worker            Check one-use generation, explicit budgets and private failure delivery offline.' \
		'make test/integration/native-smiles-worker Check real RDKit generation across the private process channel offline.' \
		'make test/unit/smiles-qualification     Check generation fixture and controlled-stall qualification failures offline.' \
		'make test/integration/provider-geometry-worker/smiles Check native generation, controlled stalls and exact host retirement offline.' \
		'make test/integration/provider-native-geometry-worker Check separate RDKit reader containers, private failures and host retirement offline.' \
		'make test/integration/rdkit-runtime     Check locked RDKit dependencies and native imports in an isolated offline environment.' \
		'make geometry/image/build              Build the offline RDKit runtime for molecular readers and conformer generation.' \
		'make provider/image/build              Build the offline installed provider controller; no runtime installation.' \
		'make provider/prepare                  Acquire dependencies/models and build images; choose cache and interface once.' \
		'make provider/deployment/install DEPLOYMENT=<name> Freeze public configuration and private inputs into an immutable Compose bundle.' \
		'make provider/deployment/up DEPLOYMENT=<name> Start the selected bundle and wait for component readiness and accepted Hello.' \
		'make provider/deployment/status DEPLOYMENT=<name> Inspect the actual deployment containers against their original bundles.' \
		'make provider/deployment/logs DEPLOYMENT=<name> Read bounded Docker logs without creating log dump files.' \
		'make provider/deployment/down DEPLOYMENT=<name> Stop provider, native runners, then proxies; retain containers and logs.' \
		'make test/integration/provider/deployment Exercise one local signed API prediction and restart through the final Compose commands (opt-in).' \
		'make test/integration/provider/deployment/references Compare every connected reference prediction with the original 001/002 packets (opt-in).' \
		'make test/integration/provider/deployment/capacity Qualify H128/C128 input/output capacity through the connected Compose path (opt-in).' \
		'make test/integration/provider/deployment/successive Run two distinct Jobs through one service, then check restart without replay (opt-in).' \
		'make test/integration/provider/deployment/recovery Check lost Engine replies, startup recovery and fenced replacement without scientific work (opt-in).' \
		'make test/unit/provider/deployment     Check private deployment bundles and successive refreshes offline.' \
		'make provider/run                      Run the provider service with explicit host and route configuration.' \
		'make provider/logs CONTAINER_ID=<id>   Read the last 200 journal messages for one full container ID, including removed workers.' \
		'make provider/recovery/inspect         Inspect one stopped Attempt store offline; no credential or route grant.' \
		'make provider/recovery/run RECORD_SHA256=...  Recover exactly one inspected record through the API-only route.' \
		'make test/unit/provider/image          Check offline image commands, committed source capture and wheel refusals.' \
		'make test/unit/provider/proxy          Check service destination grants and acquisition proxy regressions offline.' \
		'make test/integration/provider/proxy   Verify destination TLS through the actual Unix service proxy offline.' \
		'make test/integration/provider-runtime Check the installed controller image, locked dependencies and isolated imports.' \
		'make test/unit/provider/api-stage      Check private interpreter staging and configuration admission offline.' \
		'make test/unit/provider/api-client API_CLIENT_IMAGE=sha256:...  Check qualification comparison against the oracle offline.' \
		'make provider/qualify/api/pasted       Opt in to creating one persistent API Job with a scripted model and pinned science; see docs/provider.html for required selectors.' \
		'make provider/qualify/api/real-pasted  Opt in to one API Job using an explicit INTERPRETER_CONFIG and fixed pasted scientific oracle.' \
		'make provider/qualify/api/real-smiles  Opt in to one CCl conformer/API Job; check provenance and H/C coverage, without reference shift values.' \
		'make provider/qualify/api/real-smiles-multiple  Opt in to three CCCC conformers in one API Job; check each geometry and H/C coverage without reference shift values.' \
		'make provider/qualify/api/real-upload  Opt in to one XYZ Upload and API Job; compare supplied atoms and shifts with existing exact fixtures.' \
		'make provider/qualify/api/real-sdf-zip [UPLOAD_RECEIPT=...]  Opt in to one SDF ZIP Upload and API Job; an explicit original receipt resumes that reservation after storage reconciliation.' \
		'make provider/qualify/interpreter/ambiguity  Opt in to four selected-model ambiguity decisions; no API Jobs or scientific work.' \
		'make provider/qualify/interpreter/formats  Opt in to three MOL/PDB/MOL2 argument choices; no API Jobs or structure parsing.' \
		'make test/integration/rdkit-worker      Check the recorded RDKit image: exact installed lock, isolation and native extension origins.' \
		'make test/integration/mol-reader        Check supplied V2000 MOL coordinates and pre-native rejections in the recorded RDKit image.' \
		'make test/integration/sdf-reader        Check selected SDF records and all-or-nothing reading in the recorded RDKit image.' \
		'make test/integration/native-geometry-worker Check real MOL/SDF parsing across the private worker channel in the recorded RDKit image.' \
		'make test/integration/provider-scientific-worker Join real models, installed core, host retirement and local signed publication.' \
		'make test/integration/provider-scientific-worker/smiles Generate conformers, predict H/C and replay retained local Job publication.' \
		'make test/unit/provider-core           Test installed shared-core behavior offline.' \
		'make test/unit/provider-core/runner    Test fresh native children and independent lifecycle retirement offline.' \
		'make test/integration/provider-core/runner Test native task cancellation and lost-control recovery offline.' \
		'make test/unit/provider/worker Test prediction worker framing and exchange failures.' \
		'make test/integration/provider/prediction-recovery Prove one prediction replacement after a real synthetic kernel OOM (opt-in).' \
		'make test/integration/provider/logging  Prove routine/private output and post-removal journal access (opt-in).' \
		'make test/unit/provider-core/prepared-identity Test identity retention and execution recovery.' \
		'make test/integration/provider-core/prepared-identity Test retained inputs across real consumer sessions.' \
		'make test/integration/provider-core/smiles Test Job-bound SMILES choices with installed core.' \
		'make test/integration/provider-core/pasted Test exact pasted Job coordinates, units and source mapping.' \
		'make test/integration/provider-core/interpreter Test NMRNet input-choice lifecycle offline.' \
		'make test/integration/provider-core/config Test explicit provider startup configuration offline.' \
		'make test/integration/provider-scientific-worker/pasted Qualify pasted nm coordinates with isolated reader and released models.' \
		'make test/integration/provider-scientific-worker/uploads Qualify uploaded records with isolated reader and released models.' \
		'make test/integration/provider-core/runtime Test configured provider assembly against local TLS peers.' \
		'make test/integration/provider-core/admission Test service admission and fresh Job selection offline.' \
		'make test/integration/provider-core/service Test service scheduling and shutdown against local TLS peers.' \
		'make test/integration/provider-core/recovery Test explicit retained-record recovery offline.' \
		'make test/integration/provider-core/interpreter-ambiguity Test the fixed interpreter-probe mechanics offline.' \
		'make test/integration/provider-core/process Test configured foreground startup, shutdown and operator diagnostics offline.' \
		'make test/unit/provider/allocation Check native allocation and retirement ownership offline.' \
		'make test/integration/provider-core/native-factory Check partial native setup and cleanup ownership.' \
		'make test/integration/provider-service/uploads Run the local service with lazy native allocation and released prediction.' \
		'make test/integration/provider-service/formats Check supplied-format cases; FORMAT_CASES selects distinct authored names, e.g. sdf-carbon-only.' \
		'make test/integration/provider-service/recovery Check installed host inspection, exact replay and idle restart against synthetic local TLS.' \
		'make test/integration/provider-service/pasted Run the native service with pasted nm coordinates and exact replay.' \
		'make test/integration/provider-service/smiles Run the native service with three generated conformers and exact replay.' \
		'make test/integration/provider-service/references Compare the connected native service with all four original reference inputs.' \
		'make test/integration/provider-service/capacity Qualify synthetic H128/C128 service capacity and measured container resources.' \
		'make test/integration/provider-core/lifecycle Test native lifecycle fixture ownership offline.' \
		'make test/integration/provider-core/chat Test the shared chat adapter against local HTTPS peers.' \
		'make test/integration/provider-core/uploads Test verified Uploads, Job results and the proposed SECS byte-consumption boundary.' \
		'make test/unit/provider-core/normative Run required shared-core contracts.' \
		'make test/unit/provider-core/credential Test API-issued provider credential admission.' \
		'make test/unit/provider-core/verbosity Check the shared low/medium/max vocabulary in the installed package.' \
		'make test/integration/provider-core/output Check direct output settings, identity, capacity and signed publication offline.' \
		'make test/unit/provider-core/hello Test complete Hello announcements and acceptance.' \
		'make test/unit/provider-core/diagnostics Test protected core error facts and owned causes.' \
		'make test/unit/provider-core/recovery Test retained publication, read-only recovery and claim ownership.' \
		'make test/integration/provider-core/hello Test signed Hello over local HTTPS without consumer sources.' \
		'make test/unit/provider-core/transport Test bounded HTTPS exchange and retained failures.' \
		'make test/unit/provider-core/api Test signed API responses and mutation uncertainty.' \
		'make test/unit/provider-core/admission Test active storage capacity and confirmed-history preservation.' \
		'make test/unit/provider-core/uploads Test Upload input limits, acquisition and cumulative budgets.' \
		'make test/integration/provider-core/api Test API environment and request correlation over local TLS.' \
		'make test/integration/provider-core/transport Test received metadata after actual TLS body failures.' \
		'make test/unit/provider-core/adversarial Challenge untrusted shared-core input.' \
		'make test/contract/provider-core       Check installed package boundaries and pinned API wire contracts.' \
		'make test/integration/provider-core    Test the real NMRNet consumer of core.' \
		'make test/repository/provider-core     Check core guard, inventory and Make conventions.' \
		'make test/repository/native-images     Check scientific image source/wheel capture and exact metadata admission offline.' \
		'make test/integration/provider-core/compose WHEELHOUSE=<path> Check fixed Compose deployment grants and role profiles offline.' \
		'make test/repository/operator-inputs   Check literal selectors and image inspection admission offline.' \
		'make test/repository/documentation     Check HTML links, anchors and preserved historical records offline.' \
		'make test/unit/packages                Test lock staging and wheelhouse contracts offline.' \
		'make test/unit/packages/wheelhouse     Test cache publication and interactive path selection offline.' \
		'make test/repository/wheelhouse        Test selected cache paths, Make inputs and frozen build capture offline.' \
		'WHEELHOUSE accepts absolute or relative paths (from Make working directory); omitted paths prompt, with no default.' \
		'Acquisition prompts for an omitted WLAN_INTERFACE; automation must set it explicitly. No interface is selected by default.' \
		'make packages/provider-core/lock/stage Stage the dependency lock and advisory report on an explicit interface.' \
		'make packages/provider-core/lock/apply Apply a reviewed STAGE offline.' \
		'make packages/provider-core/wheelhouse Acquire controller wheels on an explicit interface.' \
		'make packages/provider-core/wheelhouse/check Verify controller wheels offline.' \
		'make test/unit/prediction-input        Run only the contained prediction-input tests.' \
		'make test/unit/bond-distances          Compare fixture bond distances with experiment.' \
		'make test/unit/baseline-prepare        Run only the contained baseline-preparation tests.' \
		'make test/unit/baseline-run            Run only the contained baseline-run tests.' \
		'make test/unit/baseline-model          Run only the contained model-contract tests.' \
		'make test/unit/baseline-launcher       Run only the contained baseline-launcher tests.' \
		'make test/repository                   Run strict repository checks; pending policies remain red.' \
		'make test/probe                        Check contained output ownership.' \
		'make checks/dependencies/download WLAN_INTERFACE=YOUR_INTERFACE  Acquire reviewed checker dependencies.' \
		'make checks/dependencies/check         Verify retained checker dependencies offline.' \
		'make checks/image/build                Build and record the offline repository checker image.' \
		'make packages/base-images/pull         Print required image provisioning guidance; refuses daemon pulls.' \
		'make packages/base-images/proxy/download Download and load the pinned proxy image into local Docker using Docker default routing.' \
		'make packages/base-images/python/download Download the pinned Debian Python worker base into local Docker using Docker default routing.' \
		'make packages/lock/base-images/pull    Print resolver image provisioning guidance; refuses daemon pulls.' \
		'make packages/lock/stage WLAN_INTERFACE=YOUR_INTERFACE  Resolve a lock and advisory report for review.' \
		'make packages/lock/apply STAGE=lock.ID  Apply the reviewed stage offline.' \
		'make packages/preparation/lock/stage WLAN_INTERFACE=YOUR_INTERFACE  Resolve preparation dependencies for review.' \
		'make packages/preparation/lock/apply STAGE=lock.ID  Apply the reviewed preparation pair offline.' \
		'make packages/wheelhouse WLAN_INTERFACE=YOUR_INTERFACE  Acquire locked packages and Uni-Core source.' \
		'make packages/wheelhouse/check         Verify the retained wheelhouse offline.' \
		'make packages/preparation/wheelhouse WLAN_INTERFACE=YOUR_INTERFACE  Acquire locked preparation wheels.' \
		'make packages/preparation/wheelhouse/check  Verify preparation wheels offline.' \
		'make packages/image                    Build the contained inference image from verified inputs.' \
		'make packages/import                   Check inference imports in the recorded image.' \
		'make weights/download ARCHIVE=weight.zip WLAN_INTERFACE=YOUR_INTERFACE  Acquire one declared archive.' \
		'make weights/check ARCHIVE=weight.zip   Verify one retained complete archive.' \
		'make weights/inspect ARCHIVE=weight.zip Inspect archive members for selection review.' \
		'make weights/extract ARCHIVE=weight.zip Extract only declared members; refuses replacement.' \
		'make baseline/prepare                  Prepare two intact labelled records per element offline.' \
		'make baseline/run RUN_ID=run-1 LABEL_ATOL=0.0001 ATOL=0 RTOL=0  Execute and verify the reference offline.' \
		'make prediction/tensor-parity           Compare label-free inputs with four upstream reference records offline.' \
		'make prediction/worker-parity           Compare released-model worker output with direct and reference results offline.' \
		'make prediction/worker-parity/batch     Compare batch predictions with the direct callable offline.' \
		'make prediction/model-parity            Load real released models and compare reference predictions offline.' \
		'make prediction/model-parity/state      Compare actual loaded H/C tensors with verified checkpoint state offline.' \
		'make prediction/qualify                 Compare tensors, released-model predictions and worker results offline.'

packages/base-images/pull:
	bash tools/container.sh pull

packages/base-images/proxy/download:
	bash tools/docker.sh download-proxy

packages/base-images/python/download:
	bash tools/container.sh download-base-image

test/unit/provider-geometry-worker:
	bash tools/container.sh test/unit/provider-geometry-worker

test/unit/provider/pdb:
	bash tools/container.sh $@

test/unit/provider/mol2:
	bash tools/container.sh $@

.PHONY: test/unit/provider/worker-startup
test/unit/provider/worker-startup:
	bash tools/container.sh $@

.PHONY: test/unit/provider/allocation
test/unit/provider/allocation:
	bash tools/container.sh $@

.PHONY: provider/prepare
provider/prepare:
	bash tools/provider_prepare.sh

.PHONY: provider/logs provider/run provider/recovery/inspect provider/recovery/run provider/qualify/api/pasted provider/qualify/api/real-pasted provider/qualify/api/real-smiles provider/qualify/api/real-smiles-multiple provider/qualify/api/real-upload provider/qualify/interpreter/ambiguity test/unit/provider/host
provider/logs:
	bash tools/provider_logs.sh

provider/run:
	bash tools/provider.sh

.PHONY: provider/deployment/install provider/deployment/up provider/deployment/status provider/deployment/logs provider/deployment/down test/unit/provider/deployment
.PHONY: test/integration/provider/deployment
test/integration/provider/deployment:
	bash tools/compose_qualification.sh

.PHONY: test/integration/provider/deployment/references test/integration/provider/deployment/capacity test/integration/provider/deployment/successive test/integration/provider/deployment/recovery
test/integration/provider/deployment/references test/integration/provider/deployment/capacity test/integration/provider/deployment/successive test/integration/provider/deployment/recovery:
	bash tools/compose_qualification.sh $(notdir $@)

provider/deployment/install provider/deployment/up provider/deployment/status provider/deployment/logs provider/deployment/down:
	bash tools/provider_deployment.sh $(notdir $@)

test/unit/provider/deployment:
	bash tools/container.sh $@

provider/recovery/inspect:
	bash tools/provider.sh recovery-inspect

provider/recovery/run:
	bash tools/provider.sh recovery-run

provider/qualify/api/pasted:
	bash tools/provider_api_qualification.sh scripted-pasted

provider/qualify/api/real-pasted:
	bash tools/provider_api_qualification.sh real-pasted

provider/qualify/api/real-smiles:
	bash tools/provider_api_qualification.sh real-smiles

provider/qualify/api/real-smiles-multiple:
	bash tools/provider_api_qualification.sh real-smiles-multiple

provider/qualify/api/real-upload:
	bash tools/provider_api_qualification.sh real-upload

.PHONY: provider/qualify/api/real-sdf-zip
provider/qualify/api/real-sdf-zip:
	bash tools/provider_api_qualification.sh real-sdf-zip

provider/qualify/interpreter/ambiguity:
	bash tools/provider_api_qualification.sh real-ambiguity

.PHONY: provider/qualify/interpreter/formats
provider/qualify/interpreter/formats:
	bash tools/provider_api_qualification.sh real-formats

test/unit/provider/host:
	bash tools/container.sh $@

.PHONY: test/unit/provider/result
test/unit/provider/result:
	bash tools/container.sh $@

.PHONY: test/unit/provider/api-stage test/unit/provider/api-client
test/unit/provider/api-stage test/unit/provider/api-client:
	bash tools/container.sh $@

test: test/unit test/repository test/contract/provider-core test/integration/provider-core

test/unit: test/unit/provider-core
	bash tools/container.sh test

test/unit/prediction-input:
	bash tools/container.sh test/unit/prediction-input

test/unit/provider:
	bash tools/container.sh test/unit/provider

test/unit/baseline-prepare:
	bash tools/container.sh test/unit/baseline-prepare

test/unit/baseline-run:
	bash tools/container.sh test/unit/baseline-run

test/unit/baseline-model:
	bash tools/container.sh test/unit/baseline-model

test/unit/baseline-launcher:
	bash tools/container.sh test/unit/baseline-launcher

test/repository:
	bash tools/container.sh test/repository

test/probe:
	bash tools/container.sh probe

checks/dependencies/download:
	bash tools/checker.sh download

checks/dependencies/check:
	bash tools/checker.sh check

checks/image/build:
	bash tools/checker.sh build

weights/download:
	bash tools/container.sh download

weights/check:
	bash tools/container.sh check

weights/inspect:
	bash tools/container.sh inspect

weights/extract:
	bash tools/container.sh extract

packages/lock/base-images/pull:
	bash tools/packages.sh pull

packages/lock/stage:
	bash tools/packages.sh stage

packages/lock/apply:
	bash tools/packages.sh apply

packages/preparation/lock/stage:
	bash tools/packages.sh stage preparation

packages/preparation/lock/apply:
	bash tools/packages.sh apply preparation

packages/wheelhouse:
	bash tools/wheelhouse.sh download

packages/wheelhouse/check:
	bash tools/wheelhouse.sh check

packages/preparation/wheelhouse:
	bash tools/wheelhouse.sh download preparation

packages/preparation/wheelhouse/check:
	bash tools/wheelhouse.sh check preparation

packages/image:
	bash tools/inference_image.sh build

packages/import:
	bash tools/inference_image.sh import

baseline/prepare:
	bash tools/baseline.sh prepare

baseline/run:
	bash tools/baseline.sh run

prediction/tensor-parity:
	bash tools/baseline.sh parity

prediction/model-parity:
	bash tools/baseline.sh model-parity

.PHONY: prediction/model-parity/state
prediction/model-parity/state:
	bash tools/baseline.sh state-parity

.PHONY: prediction/worker-parity
.PHONY: prediction/worker-parity/batch
prediction/worker-parity:
	bash tools/baseline.sh worker-parity

prediction/worker-parity/batch:
	bash tools/baseline.sh batch-worker-parity

prediction/qualify:
	$(MAKE) prediction/tensor-parity
	$(MAKE) prediction/model-parity
	$(MAKE) prediction/worker-parity

.PHONY: test/unit/bond-distances
test/unit/bond-distances:
	bash tools/container.sh test/unit/bond-distances
