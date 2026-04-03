# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from base64 import b16decode
from datetime import date
from typing import Callable, Optional
import time

import tt_flash
from tt_flash.blackhole import FlashWrite
from tt_flash.chip import TTChip
from tt_flash.error import TTError
from tt_flash.utility import CConfig


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


def set_bundle_version(version: list[int]):
    global __SEMANTIC_BUNDLE_VERSION
    __SEMANTIC_BUNDLE_VERSION = version


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


def parse_wh_image(
    chip: TTChip, boardname_to_display: str, image: bytes, mask: list[dict]
) -> list[FlashWrite]:
    """
    Parse a Wormhole image and apply mask tag handlers, returning a list of FlashWrites.

    Args:
        chip: WH chip to be written to
        boardname_to_display: boardname of the chip, used for generating error messages
        image: raw bytes read from an image file in a fwbundle
        mask: list of dicts containing start, end, and tag keys

    Returns:
        A sorted list of FlashWrites with tag handlers applied
    """
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
                    data = handler(
                        chip, data, start, start - curr_addr, end - start
                    )
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


def check_wh_can_reset(
    chip: TTChip, boardname: str, debug_messages: list[str]
) -> bool:
    """
    Check if a Wormhole board can be auto-reset after flash.

    Args:
        chip: WH chip that was flashed
        boardname: internal board name identifier
        debug_messages: list to append status messages to

    Returns:
        True if the board can be auto-reset, False otherwise
    """
    if boardname in ["NEBULA_X1", "NEBULA_X2"]:
        debug_messages.append(
            "\t\t\tBoard will require reset to complete update, checking if an automatic reset is possible"
        )
        try:
            debug_messages.append(
                f"\t\t\t\t{CConfig.COLOR.GREEN}Success:{CConfig.COLOR.ENDC} Board can be auto reset; will be triggered if the flash is successful"
            )
            return True
        except Exception as e:
            debug_messages.append(
                f"\t\t\t\t{CConfig.COLOR.YELLOW}Fail:{CConfig.COLOR.ENDC} Board cannot be auto reset: Failed to get the current firmware versions. This won't stop the flash, but will require manual reset"
            )
            return False
    elif boardname == "WH_UBB":
        return True
    else:
        return False


def nebula_x2_post_flash(
    chip: TTChip, data, debug_messages: list[str]
) -> Optional[bool]:
    """
    Handle NEBULA_X2 left-to-right SPI copy after flash.

    Args:
        chip: WH chip that was flashed
        data: FlashData containing the board identity
        debug_messages: list to append status messages to

    Returns:
        True if copy was triggered, False if not a NEBULA_X2 board, None on error
    """
    if data.idname != "NEBULA_X2":
        return False

    debug_messages.append("\t\t\tInitiating local to remote data copy")

    # There is a bug in m3 app version 5.8.0.1 where we can trigger a boot loop during the left to right copy.
    # In this condition we will disable the auto-reset before triggering the left to right copy.
    if chip.m3_fw_app_version() == (5, 8, 0, 1):
        debug_messages.append("Mitigating bootloop bug")
        triggered_reset_disable = False
        try:
            chip.arc_msg(
                chip.fw_defines["MSG_UPDATE_M3_AUTO_RESET_TIMEOUT"], arg0=0
            )
            triggered_reset_disable = True
        except Exception as e:
            debug_messages.append(
                f"\t\t\t{CConfig.COLOR.BLUE}NOTE:{CConfig.COLOR.ENDC} Failed to disable the m3 autoreset; please reboot/reset your system and flash again to initiate the left to right copy."
            )
            return None
        if triggered_reset_disable:
            time.sleep(1.0) # Wait 1 second for m3 reset to disable

    try:
        chip.arc_msg(chip.fw_defines["MSG_TRIGGER_SPI_COPY_LtoR"])
        return True
    except Exception as e:
        debug_messages.append(
            f"\t\t\t{CConfig.COLOR.BLUE}NOTE:{CConfig.COLOR.ENDC} Failed to initiate left to right copy; please reset the host to reset the board and then rerun the flash with the --force flag to complete flash."
        )
        return None
