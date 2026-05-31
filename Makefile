# Standalone interpreter resolution.  Borrowing the umbrella's
# ../RenQuant/.venv hides undeclared dependencies + environment drift, which
# defeats the "subrepo executes standalone" invariant.  Local .venv wins, then
# python3 on PATH; PYTHON=... still overrides explicitly for CI / dev convenience.
ifneq ("$(wildcard .venv/bin/python)","")
PYTHON ?= .venv/bin/python
else
PYTHON ?= python3
endif
COMMON_SRC ?= ../renquant-common/src
BASE_DATA_SRC ?= ../renquant-base-data/src
ARTIFACTS_SRC ?= ../renquant-artifacts/src
export PYTHONPATH := $(COMMON_SRC):$(BASE_DATA_SRC):$(ARTIFACTS_SRC):src:$(PYTHONPATH)

.PHONY: test doctor

test:
	$(PYTHON) -m pytest -q

doctor:
	$(PYTHON) -c "from renquant_pipeline import RuntimeInferencePipeline; from renquant_common import Pipeline; print('renquant-pipeline ok')"
