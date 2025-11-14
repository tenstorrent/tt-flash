# SPDX-FileCopyrightText: © 2025 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from base64 import b16decode
from dataclasses import dataclass
from datetime import date
from io import BufferedReader
from typing import Callable

import tt_flash
from tt_flash.chip import TTChip, WhChip
from tt_flash.error import TTError


@dataclass
class FlashWrite:
    offset: int
    write: bytearray


def rmw_param(
    chip: TTChip, data: bytearray, spi_addr: int, data_addr: int, len: int
) -> bytearray:
    # Read the existing data
    existing_data = chip.spi_read(spi_addr, len)

    # Do the RMW
    data[data_addr : data_addr + len] = existing_data

    return data


def incr_param(
    chip: TTChip, data: bytearray, spi_addr: int, data_addr: int, len: int
) -> bytearray:
    # Read the existing data
    existing_data = chip.spi_read(spi_addr, len)

    try:
        data_bytes = (int.from_bytes(existing_data, "little") + 1).to_bytes(
            len, "little"
        )
    except OverflowError:
        # If we overflow, just set it to 0
        data_bytes = (1).to_bytes(len, "little")

    # Do the RMW
    data[data_addr : data_addr + len] = data_bytes

    return data


def date_param(
    chip: TTChip, data: bytearray, spi_addr: int, data_addr: int, len: int
) -> bytearray:
    today = date.today()
    int_date = int(f"0x{today.strftime('%Y%m%d')}", 16)  # Date in 0xYYYYMMDD

    # Do the RMW
    data[data_addr : data_addr + len] = int_date.to_bytes(len, "little")

    return data


def flash_version(
    chip: TTChip, data: bytearray, spi_addr: int, data_addr: int, len: int
) -> bytearray:
    version = tt_flash.__version__

    version_parts = version.split(".")
    for _ in range(version_parts.__len__(), 4):
        version_parts.insert(0, "0")
    version_parts = version_parts[:4]

    version = [
        int(version_parts[3]),
        int(version_parts[2]),
        int(version_parts[1]),
        int(version_parts[0]),
    ]

    # Do the RMW
    data[data_addr : data_addr + len] = bytes(version)

    return data


# HACK(drosen): I don't want to update the callback function just to implement the bundle version
# but it is only set once so it's not too bad to just set it as a global.
__SEMANTIC_BUNDLE_VERSION = [0xFF, 0xFF, 0xFF, 0xFF]


def set_semantic_bundle_version(new_bundle_version: list[int]):
    global __SEMANTIC_BUNDLE_VERSION
    __SEMANTIC_BUNDLE_VERSION = new_bundle_version


def bundle_version(
    chip: TTChip, data: bytearray, spi_addr: int, data_addr: int, len: int
) -> bytearray:
    global __SEMANTIC_BUNDLE_VERSION

    for _ in range(__SEMANTIC_BUNDLE_VERSION.__len__(), 4):
        __SEMANTIC_BUNDLE_VERSION.append(0)
    version_parts = __SEMANTIC_BUNDLE_VERSION[:4]

    version = [
        int(version_parts[3]),
        int(version_parts[2]),
        int(version_parts[1]),
        int(version_parts[0]),
    ]

    # Do the RMW
    data[data_addr : data_addr + len] = bytes(version)

    return data


TAG_HANDLERS: dict[str, Callable[[TTChip, bytearray, int, int, int], bytearray]] = {
    "rmw": rmw_param,
    "incr": incr_param,
    "date": date_param,
    "flash_version": flash_version,
    "bundle_version": bundle_version,
}


def build_flash_writes_wh(
    chip: WhChip, image: BufferedReader, mask: dict, boardname_to_display: str
) -> list[FlashWrite]:
    # I expected to see a list of dicts, with the keys
    # "start", "end", "tag"
    param_handlers = []
    for v in mask:
        start = v.get("start", None)
        end = v.get("end", None)
        tag = v.get("tag", None)

        if (
            (start is None or not isinstance(start, int))
            or (end is None or not isinstance(end, int))
            or (tag is None or not isinstance(tag, str))
        ):
            raise TTError(
                f"Invalid mask format for {boardname_to_display}; expected to see a list of dicts with keys 'start', 'end', 'tag'"
            )

        if tag in TAG_HANDLERS:
            param_handlers.append(((start, end), TAG_HANDLERS[tag]))
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
    writes = []

    curr_addr = 0
    for line in image.decode("utf-8").splitlines():
        line = line.strip()
        if line.startswith("@"):
            curr_addr = int(line.lstrip("@").strip())
        else:
            data = b16decode(line)

            curr_stop = curr_addr + len(data)

            for (start, end), handler in param_handlers:
                if start < curr_stop and end > curr_addr:
                    # chip, data, spi_addr, data_addr, len
                    if not isinstance(data, bytearray):
                        data = bytearray(data)
                    data = handler(chip, data, start, start - curr_addr, end - start)
                elif start >= curr_addr and start < curr_stop and end >= curr_stop:
                    raise TTError(
                        f"A parameter write ({start}:{end}) splits a writeable region ({curr_addr}:{curr_stop}) in {boardname_to_display}! This is not supported."
                    )

            if not isinstance(data, bytes):
                data = bytes(data)
            writes.append(FlashWrite(curr_addr, data))

            curr_addr = curr_stop

    writes.sort(key=lambda x: x.offset)

    return writes
