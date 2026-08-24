# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tests for reading telemetry by tag. No hardware required.

Usage:
    pytest test_telemetry.py
"""

from types import SimpleNamespace

import pytest

from tt_flash.chip import BhChip

# Two independent pointers into CSM. The data array is deliberately placed
# below the table here, so that anything deriving one address from the other
# reads the wrong words.
SCRATCH_RAM = {12: 0x8003_0030, 13: 0x8003_0034}
DATA_ADDR = 0x1000_1000
TABLE_ADDR = 0x1000_2000


class FakeLuwenChip:
    """Enough of a Blackhole chip to serve reads out of a sparse memory."""

    def __init__(self, memory: dict[int, int]):
        self.memory = memory

    def pci_interface_id(self) -> int:
        return 0

    def axi_translate(self, path: str) -> SimpleNamespace:
        index = int(path.split("[")[1].rstrip("]"))
        return SimpleNamespace(addr=SCRATCH_RAM[index])

    def axi_read32(self, addr: int) -> int:
        return self.memory.get(addr, 0)

    def axi_read(self, addr: int, data: bytearray):
        for i in range(0, len(data), 4):
            data[i : i + 4] = self.memory.get(addr + i, 0).to_bytes(4, "little")


def telemetry_memory(entries: dict[int, int], values: dict[int, int]) -> dict[int, int]:
    """
    Lays out a telemetry table: entries maps tag -> offset, values maps
    offset -> the word at that offset in the data array.
    """
    memory = {
        SCRATCH_RAM[13]: TABLE_ADDR,
        SCRATCH_RAM[12]: DATA_ADDR,
        TABLE_ADDR: 1,  # version
        TABLE_ADDR + 4: len(entries),
    }
    for i, (tag, offset) in enumerate(entries.items()):
        memory[TABLE_ADDR + 8 + i * 4] = (offset << 16) | tag
    for offset, value in values.items():
        memory[DATA_ADDR + offset * 4] = value
    return memory


def chip(memory: dict[int, int]) -> BhChip:
    return BhChip(FakeLuwenChip(memory))


def test_read_telemetry_tag():
    device = chip(telemetry_memory({80: 5, 1: 0}, {5: 0x20BB20, 0: 0xDEADBEEF}))

    assert device.read_telemetry_tag(80) == 0x20BB20
    assert device.read_telemetry_tag(1) == 0xDEADBEEF


def test_unreported_tag():
    device = chip(telemetry_memory({1: 0}, {0: 0xDEADBEEF}))

    assert device.read_telemetry_tag(80) is None


def test_data_array_is_not_relative_to_the_table():
    """
    The data array has its own pointer and may sit anywhere; a reader that
    assumes it follows the tag table reads the wrong words, or nothing.
    """
    entries = {80: 5}
    memory = telemetry_memory(entries, {5: 0x20BB20})
    # What a reader deriving the data address from the table would land on
    table_relative = TABLE_ADDR + 8 + len(entries) * 4
    memory[table_relative + 5 * 4] = 0xBADBAD

    assert chip(memory).read_telemetry_tag(80) == 0x20BB20


def test_offset_beyond_the_entry_count():
    """
    The data array's length is not the entry count; an offset may point past
    it.
    """
    device = chip(telemetry_memory({80: 9}, {9: 0x20BB20}))

    assert device.read_telemetry_tag(80) == 0x20BB20


def test_table_is_cached():
    memory = telemetry_memory({80: 0}, {0: 0x20BB20})
    device = chip(memory)

    assert device.read_telemetry_tag(80) == 0x20BB20

    memory.clear()  # A second walk of the table would now fail
    assert device.read_telemetry_tag(80) == 0x20BB20


@pytest.mark.parametrize(
    "broken",
    [
        {SCRATCH_RAM[13]: 0},  # firmware has not published the table
        {SCRATCH_RAM[12]: 0},  # nor the data array
        {SCRATCH_RAM[13]: 0xFFFFFFFF},  # a pointer outside CSM
        {TABLE_ADDR + 4: 0},  # no entries
        {TABLE_ADDR + 4: 0xFFFFFFFF},  # an entry count that cannot be real
    ],
)
def test_unreadable_telemetry(broken):
    """A chip that cannot tell us reports no tags, rather than throwing."""
    memory = telemetry_memory({80: 0}, {0: 0x20BB20})
    memory.update(broken)

    assert chip(memory).read_telemetry_tag(80) is None
