PYTHON ?= python3

.PHONY: test lint

# tests/test_mcp.py runs when the mcp package is installed (pip install '.[mcp]'), and skips otherwise.
test:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests -v

lint:
	ruff check --no-cache --select E,F,W,B --ignore E501 --target-version py314 .
