.DEFAULT_GOAL := help

.PHONY: help packages/base-images/pull test test/unit test/repository test/probe weights/download weights/check
.PHONY: weights/inspect weights/extract
.PHONY: packages/lock/base-images/pull packages/lock/stage packages/lock/apply
.PHONY: packages/wheelhouse packages/wheelhouse/check
.PHONY: packages/image packages/import baseline/prepare baseline/run

# GNU Make implicitly exports command-line variables. Export only their literal
# value aliases so $(shell ...) in an operator selector cannot run on the host.
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/Makefile#L74-L78
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/check-g01-release-inputs.sh#L48-L49
unexport ARCHIVE STAGE WLAN_INTERFACE LABEL_ATOL ATOL RTOL RUN_ID
export NMRNET_ARCHIVE_INPUT := $(if $(filter command line,$(origin ARCHIVE)),$(value ARCHIVE))
export NMRNET_STAGE_INPUT := $(if $(filter command line,$(origin STAGE)),$(value STAGE))
export NMRNET_WLAN_INTERFACE_INPUT := $(if $(filter command line,$(origin WLAN_INTERFACE)),$(value WLAN_INTERFACE))
# tools/baseline.sh consumes explicit choices; ambient tolerances cannot qualify a run.
export NMRNET_LABEL_ATOL_INPUT := $(if $(filter command line,$(origin LABEL_ATOL)),$(value LABEL_ATOL))
export NMRNET_ATOL_INPUT := $(if $(filter command line,$(origin ATOL)),$(value ATOL))
export NMRNET_RTOL_INPUT := $(if $(filter command line,$(origin RTOL)),$(value RTOL))
export NMRNET_RUN_ID_INPUT := $(if $(filter command line,$(origin RUN_ID)),$(value RUN_ID))

help:
	@printf '%s\n' \
		'make help                              List maintained commands; also the default goal.' \
		'make test                              Run the offline unit and repository suites.' \
		'make test/unit                         Run the contained unit suite.' \
		'make test/repository                   Run strict repository checks; pending policies remain red.' \
		'make test/probe                        Check contained output ownership.' \
		'make packages/base-images/pull         Print required image provisioning guidance; refuses daemon pulls.' \
		'make packages/lock/base-images/pull    Print resolver image provisioning guidance; refuses daemon pulls.' \
		'make packages/lock/stage WLAN_INTERFACE=YOUR_INTERFACE  Resolve a lock and advisory report for review.' \
		'make packages/lock/apply STAGE=lock.ID  Apply the reviewed stage offline.' \
		'make packages/wheelhouse WLAN_INTERFACE=YOUR_INTERFACE  Acquire locked packages and Uni-Core source.' \
		'make packages/wheelhouse/check         Verify the retained wheelhouse offline.' \
		'make packages/image                    Build the contained inference image from verified inputs.' \
		'make packages/import                   Check inference imports in the recorded image.' \
		'make weights/download ARCHIVE=weight.zip WLAN_INTERFACE=YOUR_INTERFACE  Acquire one declared archive.' \
		'make weights/check ARCHIVE=weight.zip   Verify one retained complete archive.' \
		'make weights/inspect ARCHIVE=weight.zip Inspect archive members for selection review.' \
		'make weights/extract ARCHIVE=weight.zip Extract only declared members; refuses replacement.' \
		'make baseline/prepare                  Prepare two intact labelled records per element offline.' \
		'make baseline/run RUN_ID=run-1 LABEL_ATOL=0.0001 ATOL=0 RTOL=0  Execute and verify the reference offline.'

packages/base-images/pull:
	bash tools/container.sh pull

test: test/unit test/repository

test/unit:
	bash tools/container.sh test

test/repository:
	bash tools/container.sh test/repository

test/probe:
	bash tools/container.sh probe

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

packages/wheelhouse:
	bash tools/wheelhouse.sh download

packages/wheelhouse/check:
	bash tools/wheelhouse.sh check

packages/image:
	bash tools/inference_image.sh build

packages/import:
	bash tools/inference_image.sh import

baseline/prepare:
	bash tools/baseline.sh prepare

baseline/run:
	bash tools/baseline.sh run
