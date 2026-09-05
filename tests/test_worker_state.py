# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the state a pool worker sets up for itself. These do not require
hardware: a chip of a type flash_chip does not handle stands in for a real one,
so flash_chip returns before it touches anything.

A worker inherits the parent's memory only when the pool forks. Every start
method other than fork gives it a freshly imported module, and python 3.14
makes forkserver the default on linux, so anything the parent set before the
pool started has to be set again in the worker.

Usage:
    pytest tests/test_worker_state.py -v
"""

import pytest

from tt_flash import flash, wormhole
from tt_flash.flash import Manifest


class UnsupportedChip:
    """A chip flash_chip recognises as neither blackhole nor wormhole."""

    def as_bh(self) -> None:
        return None

    def as_wh(self) -> None:
        return None


@pytest.fixture()
def unflashable_chip(monkeypatch) -> None:
    monkeypatch.setattr(flash, "PciChip", lambda interface_id: UnsupportedChip())


def read_back_bundle_version() -> bytes:
    """
    The four version bytes the wh tag handler would write into an image. This
    is the only reader of the module global, so it is what the global means.
    """
    return bytes(wormhole.bundle_version(None, bytearray(4), 0, 0, 4))


def test_flash_chip_sets_the_bundle_version_it_was_given(unflashable_chip):
    wormhole.set_bundle_version([0, 0, 0, 0])

    flash.flash_chip(
        0,
        "unused.fwbundle",
        Manifest(data={}, bundle_version=(19, 6, 1, 0)),
        force=True,
        allow_major_downgrades=False,
        skip_missing_fw=True,
    )

    # The handler writes the four parts least significant first.
    assert read_back_bundle_version() == bytes([0, 1, 6, 19])


def test_flash_chip_does_not_leave_the_default_version_behind(unflashable_chip):
    """
    The default is 0xFFFFFFFF, and it verifies clean once written: the flash is
    checked against the image it came from, not against the manifest. A worker
    that never set the version would write it and report success.
    """
    wormhole.set_bundle_version([0xFF, 0xFF, 0xFF, 0xFF])

    flash.flash_chip(
        0,
        "unused.fwbundle",
        Manifest(data={}, bundle_version=(19, 6, 1, 0)),
        force=True,
        allow_major_downgrades=False,
        skip_missing_fw=True,
    )

    assert read_back_bundle_version() != b"\xff\xff\xff\xff"
