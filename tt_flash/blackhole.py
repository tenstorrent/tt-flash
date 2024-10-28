# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import ctypes

from tt_flash.boot_fs import tt_boot_fs_fd
from tt_flash.error import TTError
from . import boot_fs

from tt_flash.chip import BhChip


def writeback_boardcfg(chip: BhChip, write: bytearray) -> bytearray:
    # Find boardcfg on chip
    fd_in_spi = boot_fs.read_tag(
        lambda addr, size: chip.spi_read(addr, size), "boardcfg"
    )
    if fd_in_spi is None:
        raise TTError("Couldn't find boardcfg on chip")

    # Find boardcfg in current fd
    fd_to_flash = boot_fs.read_tag(
        lambda addr, size: write[addr : addr + size], "boardcfg"
    )
    if fd_to_flash is None:
        raise TTError("Couldn't find boardcfg in flash package")
    fd_as_data = bytes(fd_in_spi[1])
    write[fd_to_flash[0] : fd_to_flash[0] + len(fd_as_data)] = fd_as_data

    flashed_fd = boot_fs.read_tag(
        lambda addr, size: write[addr : addr + size], "boardcfg"
    )
    assert flashed_fd[1] == fd_in_spi[1], f"{flashed_fd[1]} != {fd_in_spi[1]}"

    return write


TAG_HANDLERS = {"write-boardcfg": writeback_boardcfg}


def boot_fs_write(
    chip: BhChip, boardname_to_display: str, mask: dict, write: bytearray
) -> bytearray:
    param_handlers = []
    for v in mask:
        tag = v.get("tag", None)

        if tag is None or not isinstance(tag, str):
            raise TTError(
                f"Invalid mask format for {boardname_to_display}; expected to see a list of dicts with keys 'tag'"
            )

        if tag in TAG_HANDLERS:
            param_handlers.append(TAG_HANDLERS[tag])
        else:
            if len(TAG_HANDLERS) > 0:
                pretty_tags = [f"'{x}'" for x in TAG_HANDLERS.keys()]
                pretty_tags[-1] = f"or {pretty_tags[-1]}"
                raise TTError(
                    f"Invalid tag {tag} for {boardname_to_display}; expected to see one of {pretty_tags}"
                )
            else:
                raise TTError(
                    f"Invalid tag {tag} for {boardname_to_display}; there aren't any tags defined!"
                )

    for handler in param_handlers:
        write = handler(chip, write)

    return write
