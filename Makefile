PYTHON ?= python3
PLUGIN_PYTHON ?= python3.12

.PHONY: test test-service test-plugin lint build-plugin

test: test-service test-plugin

test-service:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests -v

test-plugin:
	$(PLUGIN_PYTHON) -m unittest tests.test_plugin -v

lint:
	ruff check --no-cache --select E,F,W,B --ignore E501 --target-version py312 .

build-plugin:
	scripts/build-plugin.sh
