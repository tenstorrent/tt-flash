# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Shadow-copy scaffolding for Blackhole SPI write tests.

Blackhole flash is 64MB on every board, but the fixed partition layout ends
below 0x32F000 and everything from 5MB up is the ``storage`` partition, which
has no runtime consumer.  These helpers clone the live layout into a scratch
window up there so tests can drive real erase and program cycles through the
production flash path without ever touching the firmware region.
"""

import ctypes
from typing import Iterator, Sequence

import pytest

import bh_image
from tt_flash import boot_fs
from tt_flash.blackhole import FlashWrite
from tt_flash.chip import BhChip
from tt_flash.utility import get_board_type

SECTOR = 0x1000
FLASH_SIZE = 0x4000000  # 64MB on every Blackhole board

# Small shadow window, used by default.
#
# The base is chosen so its low 24 bits also land somewhere harmless.  Runtime
# addressing is 4-byte (the SMC devicetree sets `use-4byte-addressing` and the
# driver issues explicit 4-byte opcodes), so the real target is 40MB..48MB,
# inside the unused `storage` partition.  But if a part ever truncated to 24
# bits the window would fold to 8MB..16MB -- still `storage`, still nowhere
# near the firmware.  A base of 0x2000000 would fold onto the descriptor table
# at 0 and destroy the board; this one cannot, at any span up to 8MB.
SHADOW_SMALL_BASE = 0x2800000
SHADOW_SMALL_MAX_SPAN = 0x800000

# Large shadow window, 32MB..64MB.  NOT fold-safe: under truncation it would
# wrap through 0.  Only reachable once the truncation probe has actively ruled
# that out.  Needed for 18.x-era images, which put boardcfg at 0xfff000.
SHADOW_LARGE_BASE = 0x2000000
SHADOW_LARGE_MAX_SPAN = 0x2000000

FD_SIZE = ctypes.sizeof(boot_fs.tt_boot_fs_fd)

_PROBE_MAGIC = bytes.fromhex("a5f0c3690f5a3c96") * 4
_PROBE_CACHE: dict[int, bool] = {}


def get_board_name(device) -> str:
    """Board name used to select this device's image from a fwbundle."""
    try:
        boardname = get_board_type(device.board_type(), from_type=True)
    except Exception:
        boardname = pytest.fail(f"Board type not recognized for {device}")

    # For P300 we need to check if it's L or R chip
    if "P300" in boardname:
        # 0 = Right, 1 = Left
        if device.get_asic_location() == 0:
            boardname = f"{boardname}_right"
        elif device.get_asic_location() == 1:
            boardname = f"{boardname}_left"

    return boardname


class ShadowChip(BhChip):
    """A BhChip whose SPI accesses are relocated into a scratch window.

    Subclasses BhChip rather than wrapping it because ``flash_chip_stage1``
    dispatches on ``isinstance(chip, BhChip)``.  Everything downstream --
    ``boot_fs.read_tag`` walking from address 0, ``writeback_boardcfg``,
    ``skip_ccfgovr`` and stage 2's write/verify loop -- then lands in the
    shadow with no production code change.

    ``get_bundle_version`` is deliberately *not* redirected: it calls
    ``decode_boot_fs_table`` inside pyluwen, which walks the boot fs at
    absolute addresses.  Stage 1's version gate therefore sees the real flash,
    so callers pass ``force=True``.
    """

    def __init__(self, chip: BhChip, base: int, span: int) -> None:
        self.__dict__.update(chip.__dict__)  # reuse the live luwen handle
        self._shadow_base = base
        self._shadow_span = span

    def __repr__(self) -> str:
        return f"Shadow[{self.interface_id}]@0x{self._shadow_base:x}"

    def _guard(self, addr: int, size: int) -> int:
        assert addr >= 0 and size >= 0, f"negative shadow access {addr}+{size}"
        phys = self._shadow_base + addr
        assert phys + size <= self._shadow_base + self._shadow_span, (
            f"shadow access escaped its window: 0x{phys:x}+{size} outside "
            f"0x{self._shadow_base:x}..0x{self._shadow_base + self._shadow_span:x}"
        )
        return phys

    def spi_read(self, addr: int, size: int) -> bytes:
        return super().spi_read(self._guard(addr, size), size)

    def spi_write(self, addr: int, data: bytes) -> None:
        super().spi_write(self._guard(addr, len(data)), data)


def on_chip_fds(chip) -> Iterator[tuple[int, boot_fs.tt_boot_fs_fd]]:
    """Walk a chip's live boot fs descriptor table.

    Bounded to one sector: that is all the table physically occupies, and an
    unbounded walk over a region that is neither valid descriptors nor erased
    flash would run until it happened to hit a set `invalid` bit.
    """
    for offset in range(0, SECTOR, FD_SIZE):
        fd = boot_fs.read_fd(lambda a, s: chip.spi_read(a, s), offset)
        if fd is None or fd.flags.f.invalid != 0:
            return
        yield offset, fd


def sector_range(start: int, length: int) -> tuple[int, int]:
    """Sector-aligned range covering ``length`` bytes at ``start``."""
    return start & ~(SECTOR - 1), (start + length + SECTOR - 1) & ~(SECTOR - 1)


def coalesce(regions: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and merge overlapping or touching ranges."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(regions):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def truncation_ok(chip: BhChip) -> bool:
    """True if the part genuinely addresses above 16MB.

    Writes a magic at the fold-safe small base and checks whether it also shows
    up at that address truncated to 24 bits.  Both addresses sit in unused
    ``storage``, so the probe is harmless whichever way it goes -- which is only
    true *because* the small base is fold-safe.  Probing directly in the large
    window would have destroyed the descriptor table before it could report
    anything.
    """
    cached = _PROBE_CACHE.get(chip.interface_id)
    if cached is not None:
        return cached

    fold = SHADOW_SMALL_BASE & 0xFFFFFF
    chip.spi_write(fold, bytes(len(_PROBE_MAGIC)))
    chip.spi_write(SHADOW_SMALL_BASE, _PROBE_MAGIC)
    ok = chip.spi_read(fold, len(_PROBE_MAGIC)) != _PROBE_MAGIC

    _PROBE_CACHE[chip.interface_id] = ok
    return ok


def required_span(chip: BhChip, writes: Sequence[FlashWrite] = ()) -> int:
    """Highest SPI address the flash path will touch, rounded to a sector.

    Covers three things: the live descriptor table and the bodies it points at,
    the image's own write extents, and the addresses named by descriptors
    *inside* the image.  That last one matters because ``writeback_boardcfg``
    can append a write at an address no image section covers -- an 18.x image
    is a single blob at 0 whose boardcfg descriptor points at 0xfff000.
    """
    end = SECTOR  # the descriptor table itself
    for _, fd in on_chip_fds(chip):
        end = max(end, fd.spi_addr + fd.flags.f.image_size)
    for write in writes:
        end = max(end, write.offset + len(write.write))
        if write.offset == 0:
            for _, fd in bh_image.iter_fds(write.write):
                end = max(end, fd.spi_addr + fd.flags.f.image_size)
    return (end + SECTOR - 1) & ~(SECTOR - 1)


def clone_regions(chip: BhChip) -> list[tuple[int, int]]:
    """Regions of live flash the shadow needs a faithful copy of.

    Only the descriptor table and the bodies it points at, because those are
    the only places the flash path reads from.  The dead space between
    partitions is skipped, which keeps the clone near 1MB rather than 16MB.
    """
    regions = [(0, SECTOR)]
    for _, fd in on_chip_fds(chip):
        regions.append(sector_range(fd.spi_addr, fd.flags.f.image_size))
    return coalesce(regions)


def build_shadow(chip: BhChip, writes: Sequence[FlashWrite] = ()) -> ShadowChip:
    """Pick a window, clone the live layout into it and verify the copy."""
    span = required_span(chip, writes)

    if span <= SHADOW_SMALL_MAX_SPAN:
        base = SHADOW_SMALL_BASE
    else:
        if not truncation_ok(chip):
            pytest.skip(
                f"{chip} truncates SPI addresses to 24 bits; the large shadow "
                "window would wrap onto the descriptor table"
            )
        if span > SHADOW_LARGE_MAX_SPAN:
            pytest.skip(f"required span 0x{span:x} exceeds the large window")
        base = SHADOW_LARGE_BASE

    assert (
        base + span <= FLASH_SIZE
    ), f"shadow window 0x{base:x}+0x{span:x} runs off the end of flash"

    shadow = ShadowChip(chip, base, span)
    for start, end in clone_regions(chip):
        if start >= span:
            continue
        end = min(end, span)
        original = chip.spi_read(start, end - start)
        shadow.spi_write(start, original)
        assert (
            shadow.spi_read(start, end - start) == original
        ), f"shadow clone of 0x{start:x}..0x{end:x} did not read back"
    return shadow


def fill(chip, writes: Sequence[FlashWrite], value: int) -> None:
    """Overwrite every write's extent with a constant byte.

    Seeding with 0x00 before a flash is what makes the erase observable: NOR
    programming can only clear bits, so a byte going from 0x00 to anything
    non-zero cannot happen without a real sector erase.
    """
    for start, end in coalesce([(w.offset, w.offset + len(w.write)) for w in writes]):
        chip.spi_write(start, bytes([value]) * (end - start))
