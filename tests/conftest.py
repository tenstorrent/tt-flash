# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration and fixtures for tt_flash tests.
"""

from typing import Sequence, Union

import pytest

from bh_shadow import ShadowChip, build_shadow
from tt_flash.blackhole import FlashWrite
from tt_flash.chip import detect_chips, BhChip, WhChip


def pytest_addoption(parser):
    parser.addoption(
        "--fwbundle",
        action="store",
        default=None,
        help="Path to firmware bundle (.fwbundle)",
    )
    parser.addoption(
        "--fwbundle-prev",
        action="store",
        default=None,
        help="Path to an older firmware bundle, for cross-version flash tests",
    )


@pytest.fixture(scope="module")
def fwbundle_path(request):
    """Get path to firmware bundle from pytest option."""
    path = request.config.getoption("--fwbundle")
    if path is None:
        pytest.skip("--fwbundle not provided")
    return path


@pytest.fixture(scope="module")
def prev_fwbundle_path(request):
    """Get path to the older firmware bundle from pytest option."""
    path = request.config.getoption("--fwbundle-prev")
    if path is None:
        pytest.skip("--fwbundle-prev not provided")
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


@pytest.fixture()
def make_shadow():
    """Factory building a verified ShadowChip for a chip and image."""

    def _make(chip: BhChip, writes: Sequence[FlashWrite] = ()) -> ShadowChip:
        return build_shadow(chip, writes)

    return _make
