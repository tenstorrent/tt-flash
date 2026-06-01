# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tests for functionality of tag handlers. Requires hardware, but doesn't flash chips.

Usage:
    pytest test_tag_handlers.py --fwbundle=/path/to/bundle.fwbundle
"""

import tarfile

import pytest

from tt_flash import boot_fs
from tt_flash.blackhole import (
    CCFGOVR_TAGS,
    FlashWrite,
    parse_writes_from_image,
    skip_ccfgovr,
    writeback_boardcfg,
)
from tt_flash.chip import BhChip, TTChip
from tt_flash.utility import get_board_type


def get_board_name(device: TTChip) -> str:
    """Get board name for a device in order to grab the correct image from the fwbundle."""
    try:
        boardname = get_board_type(device.board_type(), from_type=True)
    except:
        boardname = pytest.fail(f"Board type not recognized for {device}")

    # For P300 we need to check if it's L or R chip
    if "P300" in boardname:
        # 0 = Right, 1 = Left
        if device.get_asic_location() == 0:
            boardname = f"{boardname}_right"
        elif device.get_asic_location() == 1:
            boardname = f"{boardname}_left"

    return boardname


def bh_load_flash_writes_from_fwbundle(
    chip: BhChip, fwbundle_path: str
) -> list[FlashWrite]:
    """BH only. Load FlashWrites from the fwbundle image.bin."""
    board_name = get_board_name(chip)

    with tarfile.open(fwbundle_path, "r:gz") as tar:
        image_file = tar.extractfile(f"./{board_name}/image.bin")
        if image_file is None:
            pytest.fail(f"Could not find {board_name}/image.bin in fwbundle")

        image = image_file.read()

    return parse_writes_from_image(image)

@pytest.mark.requires_hardware
class TestTagHandlers:
    def test_writeback_boardcfg_preserves_boardcfg_data(
        self, bh_chips: list[BhChip], fwbundle_path: str
    ):
        """
        BH Only.
        When using the BH tag handler writeback_boardcfg, tests that the boardcfg data
        is the same on the chip and in the modified flash writes.
        """
        for bh_chip in bh_chips:
            # Read current boardcfg from chip
            current_boardcfg_fd = boot_fs.read_tag(
                lambda addr, size: bh_chip.spi_read(addr, size), "boardcfg"
            )
            assert current_boardcfg_fd is not None, "Could not find boardcfg fd on chip"
            current_boardcfg_data = bh_chip.spi_read(
                current_boardcfg_fd[1].spi_addr,
                current_boardcfg_fd[1].flags.f.image_size,
            )

            # Load flash writes
            writes = bh_load_flash_writes_from_fwbundle(bh_chip, fwbundle_path)

            # Modify flash writes based on writeback_boardcfg
            writes = writeback_boardcfg(bh_chip, writes)

            # Read boardcfg to be flashed
            new_boardcfg_fd = None
            for write in writes:
                new_boardcfg_fd = boot_fs.read_tag(
                    lambda addr, size: write.write[addr : addr + size], "boardcfg"
                )
                if new_boardcfg_fd is not None:
                    break
            assert (
                new_boardcfg_fd is not None
            ), "Could not find boardcfg fd in modified writes"

            new_boardcfg_data = None
            for write in writes:
                if write.offset == new_boardcfg_fd[1].spi_addr:
                    new_boardcfg_data = write.write[
                        0 : new_boardcfg_fd[1].flags.f.image_size
                    ]
            assert (
                new_boardcfg_data is not None
            ), "Could not find boardcfg data in modified writes"

            assert (
                new_boardcfg_data == current_boardcfg_data
            ), "boardcfg data not preserved"

    def test_skip_ccfgovr_drops_bank_writes(
        self, bh_chips: list[BhChip], fwbundle_path: str
    ):
        """
        BH Only.
        skip_ccfgovr should remove the body FlashWrite for each ccfgovr bank
        present in the image while leaving the FD entries in place.
        """
        for bh_chip in bh_chips:
            writes = bh_load_flash_writes_from_fwbundle(bh_chip, fwbundle_path)

            bank_addrs = {}
            for tag in CCFGOVR_TAGS:
                for write in writes:
                    fd = boot_fs.read_tag(
                        lambda addr, size: write.write[addr : addr + size], tag
                    )
                    if fd is not None:
                        bank_addrs[tag] = fd[1].spi_addr
                        break

            if not bank_addrs:
                pytest.skip("fwbundle has no ccfgovr banks for this board")

            writes = skip_ccfgovr(bh_chip, writes)

            for tag, addr in bank_addrs.items():
                assert not any(w.offset == addr for w in writes), (
                    f"skip_ccfgovr left a body write for {tag} at 0x{addr:x}"
                )
                # FD entry must still be locatable in the remaining writes.
                found_fd = False
                for write in writes:
                    if boot_fs.read_tag(
                        lambda a, s: write.write[a : a + s], tag
                    ) is not None:
                        found_fd = True
                        break
                assert found_fd, f"skip_ccfgovr removed the {tag} FD entry"

    # TODO: test if boardcfg is a decodable read-only protobuf?

    def test_writeback_boardcfg_adds_boardcfg_to_writes(
        self, bh_chips: list[BhChip], fwbundle_path: str
    ):
        """
        BH only.
        When using the BH tag handler writeback_boardcfg, tests that the boardcfg data
        is added to the writes if it's not present.
        """
        for bh_chip in bh_chips:
            # Read current boardcfg from chip
            current_boardcfg_fd = boot_fs.read_tag(
                lambda addr, size: bh_chip.spi_read(addr, size), "boardcfg"
            )
            assert current_boardcfg_fd is not None, "Could not find boardcfg fd on chip"
            current_boardcfg_data = bh_chip.spi_read(
                current_boardcfg_fd[1].spi_addr,
                current_boardcfg_fd[1].flags.f.image_size,
            )

            # Load flash writes
            writes = bh_load_flash_writes_from_fwbundle(bh_chip, fwbundle_path)

            # Find boardcfg fd
            writes_boardcfg_fd = None
            for write in writes:
                writes_boardcfg_fd = boot_fs.read_tag(
                    lambda addr, size: write.write[addr : addr + size], "boardcfg"
                )
                if writes_boardcfg_fd is not None:
                    break
            assert (
                writes_boardcfg_fd is not None
            ), "Could not find boardcfg fd in flash writes"

            # Remove boardcfg data write if it's there
            for write in writes:
                if write.offset == writes_boardcfg_fd[1].spi_addr:
                    writes.remove(write)
                    break

            # Modify flash writes based on writeback_boardcfg
            writes = writeback_boardcfg(bh_chip, writes)

            # Find the new boardcfg data in the writes
            new_boardcfg_data = None
            for write in writes:
                if write.offset == writes_boardcfg_fd[1].spi_addr:
                    new_boardcfg_data = write.write[
                        0 : writes_boardcfg_fd[1].flags.f.image_size
                    ]
            assert (
                new_boardcfg_data is not None
            ), "Could not find boardcfg data in modified writes"

            assert (
                new_boardcfg_data == current_boardcfg_data
            ), "boardcfg data not preserved"
                

