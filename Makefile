VENV   ?= .venv
PY     := $(VENV)/bin/python
CONFIG ?= config/profiles.yaml

.PHONY: venv test lint check mock dummy lock clean

venv: $(VENV)/.installed
$(VENV)/.installed: pyproject.toml
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e ".[dev]"
	touch $@

test: venv
	$(PY) -m pytest

lint: venv
	$(VENV)/bin/ruff check rigbroker tests
	$(VENV)/bin/mypy

check: lint test

# Broker + web UI with mock rigs/services (never touches hardware). Ctrl-C to stop.
PORT ?= 8080
mock: venv
	$(PY) -m rigbroker --mock --allow-tx --config $(CONFIG) --port $(PORT)

# Real Hamlib (dummy rig on :4599) + simulated services, UI on :8081. Ctrl-C to stop.
dummy: venv
	rigctld -m 1 -P RIG -T 127.0.0.1 -t 4599 & echo $$! > /tmp/rb-dummy-rigctld.pid; \
	sleep 1; \
	$(PY) -m rigbroker --mock-services --allow-tx --rigctld vu=127.0.0.1:4599 --port 8081 \
	  --config $(CONFIG); \
	kill `cat /tmp/rb-dummy-rigctld.pid` 2>/dev/null; rm -f /tmp/rb-dummy-rigctld.pid

lock: venv
	$(PY) -m pip freeze --exclude-editable > requirements.lock

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
