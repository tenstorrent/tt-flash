all: build

# Python >= 3 is required, we do not support the older
# Python (I.E. 2.7)
PYTHON ?= python3
LUWEN_DIR ?= $${HOME}/work/luwen

.PHONY: build
build:
	${PYTHON} -m venv my-env
	. ./my-env/bin/activate && python -m pip install --upgrade pip
	. ./my-env/bin/activate && python -m pip install --upgrade -v --ignore-installed "pyluwen @ file://${LUWEN_DIR}/crates/pyluwen"
	. ./my-env/bin/activate && python -m pip install --upgrade -ve .

.PHONY: release
release:
	${PYTHON} -m venv my-env
	. ./my-env/bin/activate && python -m pip install --upgrade pip
	. ./my-env/bin/activate && python -m pip install --upgrade -v --ignore-installed -r requirements.txt
	. ./my-env/bin/activate && python -m pip install --upgrade -v .

.PHONY: clean
clean:
	rm -rf my-env
