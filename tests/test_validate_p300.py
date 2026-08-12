# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for validate_p300_can_be_flashed(). Uses fake chip objects
to test different configurations. Doesn't require hardware.

Usage:
    pytest test_validate_p300.py
"""

from dataclasses import dataclass
from typing import Optional

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
    # UPI as it appears in the PCI subsystem id. A chip running recovery FW has
    # a board_id of 0 and can only be identified through this.
    _pci_board_type: Optional[int] = None

    def board_id(self) -> int:
        return self._board_id

    def board_type(self) -> int:
        if self._pci_board_type is None:
            raise RuntimeError("Could not get PCI interface for this chip.")
        return self._pci_board_type

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

    def test_chip_in_recovery_pairs_with_its_sibling(self):
        """
        A chip running recovery FW reports no board id, so it can only be
        placed on the card whose other half is looking for it.
        """
        board_id = make_board_id()
        healthy = FakeTTChip(board_id, 1)
        recovery = FakeTTChip(0x0, 0, _pci_board_type=0x45)

        valid, incomplete = validate_p300_can_be_flashed([healthy, recovery])

        assert not incomplete
        assert valid == [healthy, recovery]

    def test_both_chips_in_recovery(self):
        """Neither chip can name its board, but the pair is still complete."""
        chips = [FakeTTChip(0x0, 0, 0x45), FakeTTChip(0x0, 1, 0x45)]

        valid, incomplete = validate_p300_can_be_flashed(chips)

        assert not incomplete
        assert len(valid) == 2

    def test_chip_in_recovery_alone(self):
        """A lone recovery chip is still half a card, so it is excluded."""
        valid, incomplete = validate_p300_can_be_flashed([FakeTTChip(0x0, 0, 0x45)])

        assert incomplete
        assert len(valid) == 0

    def test_non_p300_in_recovery(self):
        """Boards without a second chip are not subject to the pairing check."""
        valid, incomplete = validate_p300_can_be_flashed([FakeTTChip(0x0, 0, 0x40)])

        assert not incomplete
        assert len(valid) == 1

    def test_unidentifiable_chip(self):
        """
        A chip that answers neither way is passed through, leaving the flash
        path to report that it does not recognize the board.
        """
        valid, incomplete = validate_p300_can_be_flashed([FakeTTChip(0x0, 0)])

        assert not incomplete
        assert len(valid) == 1

    def test_asic_location_cannot_be_read(self):
        """An unreadable ASIC location excludes the board instead of raising."""
        class UnreadableLocation(FakeTTChip):
            def get_asic_location(self) -> int:
                raise RuntimeError("no telemetry")

        board_id = make_board_id()
        chips = [FakeTTChip(board_id, 0), UnreadableLocation(board_id, 1)]

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
