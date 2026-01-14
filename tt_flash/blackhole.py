# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from base64 import b16decode
from dataclasses import dataclass
from typing import Optional

from tt_flash import boot_fs
from tt_flash.chip import BhChip
from tt_flash.error import TTError


@dataclass
class FlashWrite:
    offset: int
    write: bytearray


def calculate_checksum(data: bytes) -> int:
    """
    Calculate 32-bit additive checksum for bootrom validation
    """
    calculated_checksum = 0

    if len(data) < 4:
        return 0

    for i in range(0, len(data), 4):
        value = int.from_bytes(data[i:][:4], "little")
        calculated_checksum += value

    calculated_checksum &= 0xFFFFFFFF

    return calculated_checksum


def writeback_boardcfg(chip: BhChip, writes: list[FlashWrite]) -> list[FlashWrite]:
    """
    Modify writes to flash to replace placeholder boardcfg data from the flash image
    with the existing boardcfg data on the chip. Writes back boardcfg data to the SPI
    address specified by the flash image and modifies the boardcfg boot fs fd to match
    the actual boardcfg data.

    Args:
        chip: BH chip to be written to
        writes: A list of FlashWrites created from the flash image

    Returns:
        A list of FlashWrites modified to include chip's existing boardcfg from its SPI.
    """
    # Find current boardcfg fd in SPI to modify and determine where to read boardcfg data from
    fd_in_spi = boot_fs.read_tag(
        lambda addr, size: chip.spi_read(addr, size), "boardcfg"
    )
    if fd_in_spi is None:
        raise TTError("Couldn't find boardcfg on chip")

    # Find boardcfg in current fd
    fd_to_flash = None
    boardcfg_write = None
    for write in writes:
        fd_to_flash = boot_fs.read_tag(
            lambda addr, size: write.write[addr : addr + size], "boardcfg"
        )
        if fd_to_flash is not None:
            boardcfg_write = write
            break
    if fd_to_flash is None:
        raise TTError("Couldn't find boardcfg in flash package")

    # Read back boardcfg data in SPI
    boardcfg_in_spi = chip.spi_read(
        fd_in_spi[1].spi_addr, fd_in_spi[1].flags.f.image_size
    )

    # Manipulate fd_in_spi
    fd_in_spi[1].spi_addr = fd_to_flash[1].spi_addr
    fd_in_spi[1].fd_crc = 0

    # Calculate fd checksum
    fd_chk = calculate_checksum(bytes(fd_in_spi[1])[:-4])
    fd_in_spi[1].fd_crc = fd_chk

    # Replace boardcfg fd_to_flash[1] with fd_in_spi[1] that we modified
    fd_as_data = bytes(fd_in_spi[1])
    boardcfg_write.write[fd_to_flash[0] : fd_to_flash[0] + len(fd_as_data)] = fd_as_data

    # Find boardcfg data write
    # this assumes that boardcfg has its own FlashWrite with its spi_addr as the offset
    data_write = None
    for write in writes:
        if write.offset == fd_to_flash[1].spi_addr:
            data_write = write
    if data_write is not None:
        # Replace boardcfg data to flash with boardcfg data from SPI
        data_write.write[0 : len(boardcfg_in_spi)] = boardcfg_in_spi
    else:
        # No boardcfg data write in this flash image, add it in so boardcfg is written to the correct spi_addr
        writes.append(FlashWrite(fd_to_flash[1].spi_addr, bytearray(boardcfg_in_spi)))
        writes.sort(key=lambda x: x.offset)

    flashed_fd = boot_fs.read_tag(
        lambda addr, size: boardcfg_write.write[addr : addr + size], "boardcfg"
    )
    assert flashed_fd[1] == fd_in_spi[1], f"{flashed_fd[1]} != {fd_in_spi[1]}"

    return writes


TAG_HANDLERS = {"write-boardcfg": writeback_boardcfg}


def parse_writes_from_image(image: bytes) -> list[FlashWrite]:
    """
    Parse data from an image file into a list of FlashWrites.

    Args:
        image: raw bytes read from an image file in a fwbundle

    Returns:
        A sorted list of FlashWrites corresponding to the image data
    """
    writes = []

    curr_addr = 0
    for line in image.decode("utf-8").splitlines():
        line = line.strip()
        if line.startswith("@"):  # address of a flash partition
            curr_addr = int(line.lstrip("@").strip())
        else:
            data = b16decode(line)
            curr_stop = curr_addr + len(data)
            if not isinstance(data, bytearray):
                data = bytearray(data)
            writes.append(FlashWrite(curr_addr, data))

            curr_addr = curr_stop

    writes.sort(key=lambda x: x.offset)

    return writes


def boot_fs_write(
    chip: BhChip, boardname_to_display: str, mask: list[dict], writes: list[FlashWrite]
) -> list[FlashWrite]:
    """
    Apply board-specific modifications to writes using tags from the mask. Process the mask tags to determine which tag handlers
    to apply to writes, then apply the handlers to modify writes.

    Args:
        chip: BH chip to be written to
        boardname_to_display: boardname of the chip, used for generating error messages
        mask: list of dicts containing tags
        writes: list of FlashWrites to be modified by tag handlers

    Returns:
        list of FlashWrites modified by tag handlers
    """
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
        writes = handler(chip, writes)

    return writes
