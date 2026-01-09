# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration and fixtures for tt_flash tests.
"""

import tarfile
from typing import Union

import pytest

from tt_flash.chip import detect_chips, BhChip, WhChip
from tt_flash.utility import get_board_type


def pytest_addoption(parser):
    parser.addoption(
        "--fwbundle",
        action="store",
        default=None,
        help="Path to firmware bundle (.fwbundle)",
    )


@pytest.fixture(scope="module")
def fwbundle_path(request):
    """Get path to firmware bundle from pytest option."""
    path = request.config.getoption("--fwbundle")
    if path is None:
        pytest.skip("--fwbundle not provided")
    return path


@pytest.fixture()
def devices() -> list[Union[WhChip, BhChip]]:
    """Get devices on system."""
    devices = detect_chips()
    if not devices:
        pytest.skip("No devices detected on system")
    return devices


@pytest.fixture()
def bh_chips(devices: list[Union[WhChip, BhChip]]) -> list[BhChip]:
    """Get BH devices on the system."""
    bh_chips = [device for device in devices if isinstance(device, BhChip)]
    if not bh_chips:
        pytest.skip("No BH devices detected on system")
    return bh_chips
