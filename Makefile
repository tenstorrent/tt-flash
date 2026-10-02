all: build

# Python >= 3 is required, we do not support the older
# Python (I.E. 2.7)
PYTHON ?= python3
LUWEN_DIR ?= $${HOME}/work/luwen

.PHONY: build
build:
	${PYTHON} -m venv .env
	. ./.env/bin/activate && python -m pip install --upgrade pip
	. ./.env/bin/activate && python -m pip install --upgrade --ignore-installed -ve .[dev]

# Build pyluwen from a local luwen checkout and install it over the published
# wheel that `build` pulled in. Needed to pick up luwen changes that have not
# been released yet; the force-reinstall has to come after `build`, or pip
# resolves the dependency back to PyPI.
.PHONY: local-luwen
local-luwen:
	. ./.env/bin/activate && python -m pip install maturin
	. ./.env/bin/activate && maturin build --release \
		-m ${LUWEN_DIR}/bind/pyluwen/Cargo.toml -o target/wheels
	. ./.env/bin/activate && python -m pip install --force-reinstall \
		--no-deps target/wheels/pyluwen-*.whl

.PHONY: release
release:
	${PYTHON} -m venv my-env
	. ./my-env/bin/activate && python -m pip install --upgrade pip
	. ./my-env/bin/activate && python -m pip install --upgrade -v --ignore-installed -r requirements.txt
	. ./my-env/bin/activate && python -m pip install --upgrade -v .

.PHONY: clean
clean:
	rm -rf .env
