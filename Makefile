.PHONY: packages/base-images/pull test test/probe weights/download weights/check
.PHONY: weights/inspect weights/extract
.PHONY: packages/lock/base-images/pull packages/lock/stage packages/lock/apply
.PHONY: packages/wheelhouse packages/wheelhouse/check

export ARCHIVE STAGE
export NMRNET_WLAN_INTERFACE_INPUT := $(if $(filter command line,$(origin WLAN_INTERFACE)),$(value WLAN_INTERFACE))

packages/base-images/pull:
	bash tools/container.sh pull

test:
	bash tools/container.sh test

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
