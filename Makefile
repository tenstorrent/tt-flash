all: build

PYTHON ?= python3

.PHONY: build
build:
	${PYTHON} -m venv my-env
	. ./my-env/bin/activate && ${PYTHON} -m pip install --upgrade pip
	. ./my-env/bin/activate && ${PYTHON} -m pip install --ignore-installed --upgrade $(shell ${PYTHON} bin/install_luwen.py)
	. ./my-env/bin/activate && cd tt-flash && ${PYTHON} -m pip install --upgrade --ignore-installed -ve .

.PHONY: clean
clean:
	rm -rf my-env
