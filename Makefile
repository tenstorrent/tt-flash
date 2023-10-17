all: build

# Python >= 3 is required, we do not support the older
# Python (I.E. 2.7)
PYTHON ?= python3

.PHONY: build
build:
	${PYTHON} -m venv my-env
	. ./my-env/bin/activate && python -m pip install --upgrade pip
	. ./my-env/bin/activate && python -m pip install --upgrade -v --ignore-installed -r requirements.txt
	. ./my-env/bin/activate && python -m pip install --upgrade -ve .

.PHONY: clean
clean:
	rm -rf my-env
