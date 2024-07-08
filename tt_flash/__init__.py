# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

# Adapted from https://github.com/python-poetry/poetry/issues/273#issuecomment-1877789967
# This will get the semantic version from the current pyproject package definition.

from typing import Any

try:
    import importlib.metadata as importlib_metadata
except ModuleNotFoundError:
    import importlib_metadata
from pathlib import Path

__package_version = "unknown"


def __get_package_version() -> str:
    """Find the version of this package."""
    global __package_version

    if __package_version != "unknown":
        # We already set it at some point in the past,
        # so return that previous value without any
        # extra work.
        return __package_version

    try:
        # Try to get the version of the current package if
        # it is running from a distribution.
        __package_version = importlib_metadata.version("tt-flash")
    except importlib.metadata.PackageNotFoundError:
        # Fall back on getting it from a local pyproject.toml.
        # This works in a development environment where the
        # package has not been installed from a distribution.
        try:
            # This gets added to the standard library as tomllib in Python3.11
            # therefore we expect to hit a ModuleNotFoundError.
            import tomli as toml
        except ModuleNotFoundError:
            import tomllib as toml

        pyproject_toml_file = Path(__file__).parent.parent / "pyproject.toml"
        if pyproject_toml_file.exists() and pyproject_toml_file.is_file():
            __package_version = toml.loads(pyproject_toml_file.read_text())["project"][
                "version"
            ]
            # Indicate it might be locally modified or unreleased.
            __package_version = __package_version + "+"

    return __package_version


def __getattr__(name: str) -> Any:
    """Get package attributes."""
    if name == "__version__":
        return __get_package_version()
    else:
        raise AttributeError(f"No attribute {name} in module {__name__}.")
