override SHELL := bash
override .SHELLFLAGS := -eu -o pipefail -c
override .DEFAULT_GOAL := help

.PHONY: help packages/base-images/pull test test/unit test/repository test/probe weights/download weights/check
.PHONY: packages/base-images/proxy/download packages/base-images/python/download
.PHONY: test/unit/prediction-input test/unit/baseline-prepare test/unit/baseline-run test/unit/baseline-launcher
.PHONY: test/unit/provider
.PHONY: test/unit/provider-geometry-worker
.PHONY: test/unit/provider-core test/unit/provider-core/normative test/unit/provider-core/adversarial
.PHONY: test/contract/provider-core test/integration/provider-core
.PHONY: test/integration/provider-worker
.PHONY: test/integration/provider-scientific-worker
.PHONY: test/integration/provider-geometry-worker
.PHONY: test/integration/provider-native-geometry-worker
.PHONY: test/integration/rdkit-runtime
.PHONY: geometry/image/build
.PHONY: test/integration/rdkit-worker
.PHONY: test/integration/mol-reader
.PHONY: test/integration/sdf-reader
.PHONY: test/integration/native-geometry-worker

test/integration/native-geometry-worker:
	bash tools/container.sh $@

test/integration/sdf-reader:
	bash tools/container.sh $@

test/integration/mol-reader:
	bash tools/container.sh $@

test/integration/rdkit-worker:
	bash tools/container.sh $@

geometry/image/build:
	bash tools/container.sh $@

test/integration/rdkit-runtime:
	bash tools/container.sh $@

test/integration/provider-geometry-worker:
	bash tools/worker_qualification.sh geometry-success
	bash tools/worker_qualification.sh geometry-timeout

test/integration/provider-native-geometry-worker:
	bash tools/worker_qualification.sh geometry-native-mol
	bash tools/worker_qualification.sh geometry-native-sdf
	bash tools/worker_qualification.sh geometry-native-rejected
	bash tools/worker_qualification.sh geometry-native-failure
	bash tools/worker_qualification.sh geometry-native-timeout

test/integration/provider-scientific-worker:
	bash tools/worker_qualification.sh scientific

test/integration/provider-worker:
	bash tools/worker_qualification.sh unstarted
	bash tools/worker_qualification.sh success
	bash tools/worker_qualification.sh grant-failure
.PHONY: test/repository/provider-core
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

test/unit/provider-core:
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
override export NMRNET_ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE))
override export NMRNET_STAGE_INPUT := $(if $(filter command line,$(origin STAGE)),$(value STAGE))
override export NMRNET_WLAN_INTERFACE_INPUT := $(if $(filter command line,$(origin WLAN_INTERFACE)),$(value WLAN_INTERFACE))
# tools/baseline.sh consumes explicit choices; ambient tolerances cannot qualify a run.
override export NMRNET_LABEL_ATOL_INPUT := $(if $(filter command line,$(origin LABEL_ATOL)),$(value LABEL_ATOL))
override export NMRNET_ATOL_INPUT := $(if $(filter command line,$(origin ATOL)),$(value ATOL))
override export NMRNET_RTOL_INPUT := $(if $(filter command line,$(origin RTOL)),$(value RTOL))
override export NMRNET_RUN_ID_INPUT := $(if $(filter command line,$(origin RUN_ID)),$(value RUN_ID))

help:
	@printf '%s\n' \
		'make help                              List maintained commands; also the default goal.' \
		'make test                              Run the offline unit and repository suites.' \
		'make test/unit                         Run the contained unit suite.' \
		'make test/unit/provider                Run only the contained provider-input tests.' \
		'make test/unit/provider-geometry-worker Run only the contained structure-reading process tests.' \
		'make test/integration/provider-worker Qualify exact-container retirement and controller confirmation offline.' \
		'make test/integration/provider-geometry-worker Check separate structure reading and retirement of a stuck reader offline.' \
		'make test/integration/provider-native-geometry-worker Check separate RDKit reader containers, private failures and host retirement offline.' \
		'make test/integration/rdkit-runtime     Check locked RDKit dependencies and native imports in an isolated offline environment.' \
		'make geometry/image/build              Build the offline RDKit runtime for molecular readers and conformer generation.' \
		'make test/integration/rdkit-worker      Check the recorded RDKit image: exact installed lock, isolation and native extension origins.' \
		'make test/integration/mol-reader        Check supplied V2000 MOL coordinates and pre-native rejections in the recorded RDKit image.' \
		'make test/integration/sdf-reader        Check selected SDF records and all-or-nothing reading in the recorded RDKit image.' \
		'make test/integration/native-geometry-worker Check real MOL/SDF parsing across the private worker channel in the recorded RDKit image.' \
		'make test/integration/provider-scientific-worker Join real models, installed core, host retirement and local signed publication.' \
		'make test/unit/provider-core           Test installed shared-core behavior offline.' \
		'make test/unit/provider-core/normative Run required shared-core contracts.' \
		'make test/unit/provider-core/adversarial Challenge untrusted shared-core input.' \
		'make test/contract/provider-core       Check installed package boundaries.' \
		'make test/integration/provider-core    Test the real NMRNet consumer of core.' \
		'make test/repository/provider-core     Check core guard, inventory and Make conventions.' \
		'make test/unit/packages                Test lock staging and wheelhouse contracts offline.' \
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
		'make prediction/model-parity            Load real released models and compare reference predictions offline.' \
		'make prediction/qualify                 Compare tensors, released-model predictions and worker results offline.'

packages/base-images/pull:
	bash tools/container.sh pull

packages/base-images/proxy/download:
	bash tools/docker.sh download-proxy

packages/base-images/python/download:
	bash tools/container.sh download-base-image

test/unit/provider-geometry-worker:
	bash tools/container.sh test/unit/provider-geometry-worker

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

.PHONY: prediction/worker-parity
prediction/worker-parity:
	bash tools/baseline.sh worker-parity

prediction/qualify:
	$(MAKE) prediction/tensor-parity
	$(MAKE) prediction/model-parity
	$(MAKE) prediction/worker-parity

.PHONY: test/unit/bond-distances
test/unit/bond-distances:
	bash tools/container.sh test/unit/bond-distances
