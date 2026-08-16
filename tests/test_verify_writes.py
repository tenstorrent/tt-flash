# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the SPI readback comparison behind `tt-flash verify` (issue #106).

verify_writes() was previously a closure inside flash_chip_stage2, reachable
only by flashing. It is now module scope so verify can reuse it, and these
cover the comparison itself with a stub chip.

No hardware required: verify_writes only calls spi_read, which is the whole
reason it can be exercised this way.

Usage:
    pytest tests/test_verify_writes.py
"""

import pytest

from tt_flash.blackhole import FlashWrite
from tt_flash.flash import verify_writes


class StubChip:
    """Minimal stand-in for a TTChip that records how it was accessed.

    spi_write is present only so the tests can assert it is never called.
    """

    def __init__(self, contents: dict[int, bytes] | None = None):
        self.contents = contents or {}
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, bytes]] = []

    def spi_read(self, offset: int, length: int) -> bytearray:
        self.reads.append((offset, length))
        data = self.contents.get(offset, b"\x00" * length)
        return bytearray(data[:length])

    def spi_write(self, offset: int, data) -> None:
        self.writes.append((offset, data))


def w(offset: int, data: bytes) -> FlashWrite:
    return FlashWrite(offset=offset, write=bytearray(data))


class TestMatching:
    def test_identical_contents_report_no_mismatch(self):
        writes = [w(0x1000, b"\xde\xad\xbe\xef")]
        chip = StubChip({0x1000: b"\xde\xad\xbe\xef"})
        assert verify_writes(chip, writes) is None

    def test_several_writes_all_matching(self):
        writes = [w(0x00, b"abcd"), w(0x10, b"efgh"), w(0x20, b"ijkl")]
        chip = StubChip({0x00: b"abcd", 0x10: b"efgh", 0x20: b"ijkl"})
        assert verify_writes(chip, writes) is None

    def test_no_writes_is_not_a_mismatch(self):
        assert verify_writes(StubChip(), []) is None

    def test_reads_exactly_the_expected_regions(self):
        writes = [w(0x1000, b"abcd"), w(0x2000, b"ef")]
        chip = StubChip({0x1000: b"abcd", 0x2000: b"ef"})
        verify_writes(chip, writes)
        assert chip.reads == [(0x1000, 4), (0x2000, 2)]


class TestMismatches:
    def test_single_differing_byte(self):
        writes = [w(0x1000, b"\xde\xad\xbe\xef")]
        chip = StubChip({0x1000: b"\xde\xad\xbe\x00"})
        assert verify_writes(chip, writes) == (3, 1)

    def test_first_mismatch_index_is_the_earliest(self):
        writes = [w(0, b"\x01\x02\x03\x04")]
        chip = StubChip({0: b"\x01\xff\x03\xff"})
        first, count = verify_writes(chip, writes)
        assert first == 1
        assert count == 2

    def test_completely_different_contents(self):
        writes = [w(0, b"\x01\x02\x03\x04")]
        chip = StubChip({0: b"\xff\xff\xff\xff"})
        assert verify_writes(chip, writes) == (0, 4)

    def test_mismatch_in_a_later_write_is_found(self):
        writes = [w(0x00, b"abcd"), w(0x10, b"efgh")]
        chip = StubChip({0x00: b"abcd", 0x10: b"efgX"})
        assert verify_writes(chip, writes) == (3, 1)

    def test_stops_at_the_first_bad_write(self):
        """Later regions are not read once a mismatch is found."""
        writes = [w(0x00, b"abcd"), w(0x10, b"efgh")]
        chip = StubChip({0x00: b"abcX", 0x10: b"efgh"})
        verify_writes(chip, writes)
        assert chip.reads == [(0x00, 4)]

    def test_unwritten_region_reads_back_as_mismatch(self):
        """A region the stub does not know about reads as zeroes."""
        writes = [w(0x9999, b"\x01\x02")]
        assert verify_writes(StubChip(), writes) == (0, 2)


class TestReadOnly:
    """The safety property: verify must never touch the flash."""

    def test_matching_run_never_writes(self):
        chip = StubChip({0: b"abcd"})
        verify_writes(chip, [w(0, b"abcd")])
        assert chip.writes == []

    def test_mismatching_run_never_writes(self):
        chip = StubChip({0: b"abcd"})
        verify_writes(chip, [w(0, b"wxyz")])
        assert chip.writes == []

    def test_stub_would_have_recorded_a_write(self):
        """Guard: the assertions above are only meaningful if a write registers."""
        chip = StubChip()
        chip.spi_write(0, b"x")
        assert chip.writes == [(0, b"x")]
