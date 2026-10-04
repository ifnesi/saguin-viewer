# saguin-viewer holds two viewers for one broker, and they share nothing but
# the numbers they read. `web` is a Flask application serving static React,
# for somebody with a browser; `cli` is a Go binary for a headless box with
# neither a browser nor a Prometheus. Each is its own module with its own
# dependencies, so each has its own targets below and neither's contributor
# has to install the other's toolchain.
.PHONY: help test go build viewer clean

help:
	@echo "test    run both suites"
	@echo "go      build and test the terminal viewer"
	@echo "build   build the terminal viewer into bin/"
	@echo "viewer  run the web viewer's suite; needs .venv (see web/README.md)"
	@echo "clean   remove build output"

test: go viewer

# **The Go suite starts real brokers**, so it needs saguin's own checkout to
# build one from - SAGUIN_REPO, or a sibling ../saguin with bin/saguin built.
# Those cases skip and say so where there is none, which is a weaker run rather
# than a passing one: they are what holds the parser and the shipped dashboard
# to a broker rather than to a document.
go: build
	go vet ./...
	go test ./...

build:
	go build -o bin/saguin-viewer ./cmd/saguin-viewer

# **The web viewer's suite needs a virtualenv, and says so rather than
# skipping.** A suite that passes quietly when its dependencies are missing is
# the defect it exists to catch, one layer up.
VENV := $(CURDIR)/.venv

viewer:
	@if [ ! -x "$(VENV)/bin/python" ]; then \
	  echo "no $(VENV) - the web viewer's suite needs it:" >&2; \
	  echo "    python3 -m venv .venv" >&2; \
	  echo "    .venv/bin/pip install -r web/requirements.txt" >&2; \
	  exit 1; \
	fi
	@"$(VENV)/bin/python" -c "import paho.mqtt, flask, yaml" 2>/dev/null || { \
	  echo "$(VENV) is missing the viewer's dependencies:" >&2; \
	  echo "    .venv/bin/pip install -r web/requirements.txt" >&2; \
	  exit 1; \
	}
	@command -v node >/dev/null 2>&1 || { \
	  echo "node is not on PATH; it drives the viewer's notify decision table" >&2; \
	  exit 1; }
# **No pipe here, and that is the whole of it.** make judges a recipe line by
# the exit of the last command in a pipeline, so filtering this suite's output
# through `grep` threw the suite's own exit away: in saguin's tree this target
# printed `FAILED (failures=1)` and exited 0, and the CI job that ran it was
# green over a red suite. It had been incapable of failing since the day it was
# written, and the first failure it ever had, it hid. A test in the suite -
# TheViewerTargetCannotSwallowItsSuite - reads this recipe and holds it to
# that, so the guard came across with the code it guards.
	@cd web && "$(VENV)/bin/python" -m unittest discover -s tests -t .
	@cd web && node --check static/app.js && echo "  app.js parses"

clean:
	rm -rf bin
