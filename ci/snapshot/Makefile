# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

################################################################

ALL_PYTHON = $(wildcard \
		*.py \
		secretvalue \
		stackoutput \
		snapshot-canary \
		snapshot-create \
		snapshot-deploy \
		snapshot-display-stacks \
		snapshot-propose \
		snapshot-update \
		snapshot-variable \
		proof \
		)

SNAPSHOT_PYTHON = $(filter-out cbmc_ci_%.py, $(ALL_PYTHON))

pylint:
	pylint --disable=missing-docstring,duplicate-code \
	       '--module-rgx=[a-zA-Z0-9_-]+' \
		$(SNAPSHOT_PYTHON)

pylint-all:
	echo $(ALL_PYTHON)
	pylint --disable=missing-docstring,duplicate-code \
		$(ALL_PYTHON)


clean:
	$(RM) *~ .*~ \#* .\#*

veryclean: clean
	$(RM) -r snapshot-[0-9]*-[0-9]* snapshot.json
