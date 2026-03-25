# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for validate_p300_can_be_flashed(). Uses fake chip objects
to test different configurations. Doesn't require hardware.

Usage:
    pytest test_validate_p300.py
"""

from dataclasses import dataclass

import pytest

from tt_flash.chip import validate_p300_can_be_flashed


def make_board_id(upi: int = 0x45, serial: int = 0x1) -> int:
    """Build a board_id with a given UPI and rest of serial number."""
    return (upi << 36) | serial


@dataclass
class FakeTTChip:
    """Stand-in for TTChip with only the methods used in validate_p300_can_be_flashed."""
    _board_id: int
    _asic_location: int

    def board_id(self) -> int:
        return self._board_id

    def get_asic_location(self) -> int:
        return self._asic_location


class TestValidateP300:
    """Tests for validate_p300_can_be_flashed()."""

    def test_complete_p300(self):
        """One P300 with L and R chips should pass."""
        board_id =  make_board_id()
        devices = [FakeTTChip(board_id, 0), FakeTTChip(board_id, 1)]

        valid, incomplete = validate_p300_can_be_flashed(devices)

        assert not incomplete
        assert len(valid) == 2

    def test_single_chip_only(self):
        """One P300 chip detected alone should be excluded."""
        board_id =  make_board_id()
        devices = [FakeTTChip(board_id, 0)]

        valid, incomplete = validate_p300_can_be_flashed(devices)

        assert incomplete
        assert len(valid) == 0

    def test_non_p300(self):
        """Non-P300 devices should always pass."""
        board_id =  make_board_id(upi = 0x40)
        devices = [FakeTTChip(board_id, 0)]

        valid, incomplete = validate_p300_can_be_flashed(devices)

        assert not incomplete
        assert len(valid) == 1

    def test_incomplete_p300_and_non_p300(self):
        """Non-P300 devices pass, even when a P300 is incomplete."""
        p300_board_id = make_board_id()
        other_board_id = make_board_id(upi = 0x40)
        devices = [
            FakeTTChip(p300_board_id, 0),
            FakeTTChip(other_board_id, 0),
        ]

        valid, incomplete = validate_p300_can_be_flashed(devices)

        assert incomplete
        assert len(valid) == 1
        assert valid[0]._board_id == other_board_id

    def test_one_complete_one_incomplete_p300(self):
        """Two P300 boards: one complete, one missing a chip."""
        board_id_a = make_board_id(serial=0x1)
        board_id_b = make_board_id(serial=0x2)
        chips = [
            FakeTTChip(board_id_a, 0), FakeTTChip(board_id_a, 1),
            FakeTTChip(board_id_b, 0),
        ]

        valid, incomplete = validate_p300_can_be_flashed(chips)

        assert incomplete
        assert len(valid) == 2
        assert all(c._board_id == board_id_a for c in valid)

    def test_duplicate_asic_location(self):
        """Two chips with same board_id but both report asic location 0."""
        board_id = make_board_id()
        chips = [FakeTTChip(board_id, 0), FakeTTChip(board_id, 0)]

        valid, incomplete = validate_p300_can_be_flashed(chips)

        assert incomplete
        assert len(valid) == 0

    def test_three_chips_same_board_id(self):
        """Three chips sharing a board_id should be rejected."""
        board_id = make_board_id()
        chips = [FakeTTChip(board_id, 0), FakeTTChip(board_id, 1), FakeTTChip(board_id, 0)]

        valid, incomplete = validate_p300_can_be_flashed(chips)

        assert incomplete
        assert len(valid) == 0

    def test_p300_variants(self):
        """All P300 UPI variants (A, B, C) should be detected as P300 and be rejected if board incomplete."""
        for upi in (0x44, 0x45, 0x46):  # P300B, P300A, P300C
            board_id = make_board_id(upi=upi)
            chips = [FakeTTChip(board_id, 0)]

            valid, incomplete = validate_p300_can_be_flashed(chips)

            assert incomplete, f"UPI {upi:#x} should be recognized as P300"
            assert len(valid) == 0
