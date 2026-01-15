# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tests for flash operations. Requires hardware and will flash chips.

Usage:
    pytest test_flash.py --fwbundle=/path/to/bundle.fwbundle -v
"""

import subprocess
from typing import Union

import pytest

from tt_flash.chip import BhChip, WhChip, detect_chips


class TestFlash:

    def test_flash_preserves_board_id(
        self, devices: list[Union[BhChip, WhChip]], fwbundle_path: str
    ):
        """
        Flash all chips and verify board_id is preserved.
        """
        # Store original board_id for each chip
        original_board_ids = {}
        for device in devices:
            telemetry = device.get_telemetry()
            board_id = telemetry.board_id
            asic_id = telemetry.asic_id_high << 32 | telemetry.asic_id_low & 0xFFFFFFFF
            original_board_ids[asic_id] = board_id

        # Flash using tt-flash CLI command
        result = subprocess.run(
            ["tt-flash", fwbundle_path, "--force"],
            capture_output=True,
            text=True,
        )

        assert (
            result.returncode == 0
        ), f"tt-flash failed with code {result.returncode}: {result.stderr}"

        # Grab new board_ids
        new_devices = detect_chips()
        new_board_ids = {}
        for device in new_devices:
            telemetry = device.get_telemetry()
            board_id = telemetry.board_id
            asic_id = telemetry.asic_id_high << 32 | telemetry.asic_id_low & 0xFFFFFFFF
            new_board_ids[asic_id] = board_id

        # Verify board_id is preserved for each chip
        assert original_board_ids == new_board_ids, (
            f"Board IDs changed after flash!\n",
            f"Before: {original_board_ids}\n",
            f"After: {new_board_ids}\n",
        )
