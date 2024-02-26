# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from base64 import b16decode
from datetime import date
import json
import signal
import tarfile
import time
from typing import Callable
import sys

import tt_flash
from tt_flash.chip import TTChip, GsChip
from tt_flash.error import TTError
from tt_flash.utility import change_to_public_name

from tt_tools_common.wh_reset import WHChipReset


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


def live_countdown(wait_time: float, name: str):
    print(f"{name} started, will wait {wait_time} seconds for it to complete")

    # If True then we are running in an interactive environment
    if sys.stdout.isatty():
        start = time.time()
        elapsed = time.time() - start
        while elapsed < wait_time:
            print(
                f"\r\033[K{name} ongoing, waiting {wait_time - elapsed:.1f} more seconds for it to complete",
                end="",
                flush=True,
            )

            time.sleep(0.1)
            elapsed = time.time() - start
        print(f"\r\033[K{name} completed", flush=True)
    else:
        time.sleep(wait_time)
        print(f"{name} completed")


def flash_chip(
    chip: TTChip,
    boardname: str,
    fw_package: tarfile.TarFile,
    force: bool,
) -> int:
    manifest_data = fw_package.extractfile("./manifest.json")
    if manifest_data is None:
        raise TTError(
            "Could not find manifest in fw package, please check the the correct one was used."
        )
    manifest = json.loads(manifest_data.read())

    manifest_bundle_version = manifest.get("bundle_version", {})

    try:
        chip.arc_msg(
            chip.fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=True, timeout=0.1
        )
    except Exception as err:
        # Ok to keep going if there's a timeout
        pass

    running_bundle_version = None
    spi_bundle_version = None
    new_bundle_version = (
        manifest_bundle_version.get("fwId", 0),
        manifest_bundle_version.get("releaseId", 0),
        manifest_bundle_version.get("patch", 0),
        manifest_bundle_version.get("debug", 0),
    )

    global __SEMANTIC_BUNDLE_VERSION
    __SEMANTIC_BUNDLE_VERSION = list(new_bundle_version)

    old_fw = False
    bundle_version = None
    exception = None
    try:
        fw_version = chip.arc_msg(
            chip.fw_defines["MSG_TYPE_FW_VERSION"], wait_for_done=True, arg0=0, arg1=0
        )[0]

        # Pre fw version 5 we don't have bundle support
        # this version of tt-flash only works with bundled fw
        # so it's safe to assume that we need to update
        if fw_version < chip.min_fw_version():
            old_fw = True
        else:
            running_bundle_version = chip.arc_msg(
                chip.fw_defines["MSG_TYPE_FW_VERSION"],
                wait_for_done=True,
                arg0=1,
                arg1=0,
            )[0]

            if running_bundle_version == 0xDEAD:
                old_fw = True
            else:
                spi_bundle_version = chip.arc_msg(
                    chip.fw_defines["MSG_TYPE_FW_VERSION"],
                    wait_for_done=True,
                    arg0=2,
                    arg1=0,
                )[0]

    except Exception as e:
        # Very old fw doesn't have support for getting the fw version at all
        # so it's safe to assume that we need to update
        old_fw = True
        exception = e

    if old_fw:
        if exception is None:
            if force:
                print(
                    "Looks like you are running a very old set of fw, assuming that it needs an update"
                )
            else:
                raise TTError(
                    "Looks like you are running a very old set of fw, it's safe to assume that it needs an update but please update it using --force"
                )
        else:
            if force:
                print(
                    f"Hit error {exception} while trying to determine running firmware. Falling back to assuming that it needs an update"
                )
            else:
                raise TTError(
                    f"Hit error {exception} while trying to determine running firmware. If you know what you are doing you may still update by re-rerunning using the --force flag."
                )

        print(f"Now flashing tt-flash version: {new_bundle_version}")
    elif running_bundle_version is not None:
        patch = running_bundle_version & 0xFF
        minor = (running_bundle_version >> 8) & 0xFF
        major = (running_bundle_version >> 16) & 0xFF
        component = (running_bundle_version >> 24) & 0xFF
        bundle_version = (component, major, minor, patch)
        if component != new_bundle_version[0]:
            if force:
                print(
                    "Found unexpected bundle version, however you ran with force so we are barreling onwards"
                )
            else:
                raise TTError(
                    f"Bundle fwId ({new_bundle_version[0]}) does not match expected fwId ({component}); {new_bundle_version} != {component}"
                )

        print(
            f"ROM version is: {bundle_version}. tt-flash version is: {new_bundle_version}"
        )
    if force:
        print("Forced ROM update requested. ROM will now be updated.")
    elif bundle_version is None:
        if spi_bundle_version is not None and spi_bundle_version >= new_bundle_version:
            if spi_bundle_version == new_bundle_version:
                print(
                    "ROM does not need to be updated, while the chip is running old FW the SPI is up to date. You can load the new firmware after a reboot, or in the case of WH a reset. Or skip this check with --force."
                )
            else:
                print(
                    "ROM does not need to be updated, while the chip is running old FW the SPI is ahead of the firmware you are attempting to flash. You can load the newer firmware after a reboot, or in the case of WH a reset. Or skip this check with --force."
                )
            return 0
        else:
            print(
                "Was not able to fetch current firmware information, assuming that it needs an update"
            )
    elif (
        bundle_version >= new_bundle_version
        and spi_bundle_version == new_bundle_version
    ):
        print(
            "ROM does not need to be updated, while the chip is running old FW the SPI is up to date. You can load the new firmware after a reboot, or in the case of WH a reset. Or skip this check with --force."
        )
        return 0
    elif bundle_version >= new_bundle_version and running_bundle_version not in [
        0xFFFFFFFF,
        0xDEAD,
    ]:
        print("ROM does not need to be updated.")
        try:
            chip.arc_msg(
                chip.fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=True, timeout=0.1
            )
        except TTError as err:
            # Ok to keep going if there's a timeout
            pass
        return 0
    else:
        print("tt-flash version > ROM version. ROM will now be updated.")

    try:
        image = fw_package.extractfile(f"./{boardname}/image.bin")
    except KeyError:
        # If file is not found then key error is raised
        image = None
    try:
        mask = fw_package.extractfile(f"./{boardname}/mask.json")
    except KeyError:
        # If file is not found then key error is raised
        mask = None

    boardname_to_display = change_to_public_name(boardname)
    if image is None and mask is None:
        raise TTError(
            f"Could not find flash data for {boardname_to_display} in tarfile"
        )
    elif image is None:
        raise TTError(
            f"Could not find flash image for {boardname_to_display} in tarfile; expected to see {boardname}/image.bin"
        )
    elif mask is None:
        raise TTError(
            f"Could not find param data for {boardname_to_display} in tarfile; expected to see {boardname}/mask.json"
        )

    # First we verify that the format of mask is valid so we don't partially flash before discovering that the mask is invalid
    mask = json.loads(mask.read())

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

    # Now we load the image and start replacing parameters
    image = image.read()

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
            writes.append((curr_addr, data))

            curr_addr = curr_stop

    print("Programming chip")

    # Install sigint handler
    def signal_handler(sig, frame):
        print("Ctrl-C: this process should not be interrupted")

    original_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        for addr, data in writes:
            chip.spi_write(addr, data)
    finally:
        signal.signal(signal.SIGINT, original_sigint_handler)

    error = False
    if boardname == "NEBULA_X2":
        print("This board is an n300, copying data over to the remote chip")
        trigged_copy = False

        # There is a bug in m3 app version 5.8.0.1 where we can trigger a boot loop during the left to right copy.
        # In this condition we will disable the auto-reset before triggering the left to right copy.
        if chip.m3_fw_app_version() == (5, 8, 0, 1):
            print("Mitiating bootloop bug")
            triggered_reset_disable = False
            try:
                chip.arc_msg(
                    chip.fw_defines["MSG_UPDATE_M3_AUTO_RESET_TIMEOUT"], arg0=0
                )
                triggered_reset_disable = True
            except Exception as e:
                print(
                    "Failed to disable the m3 autoreset please reboot/reset your system and flash again to initiate the left to right copy."
                )
                error = True
            if triggered_reset_disable:
                live_countdown(1.0, "Disable m3 reset")

        try:
            chip.arc_msg(chip.fw_defines["MSG_TRIGGER_SPI_COPY_LtoR"])
            trigged_copy = True
        except Exception as e:
            error = True
            print(
                "Failed to initiate left to right copy; please reset the host to reset the board and then rerun the flash with the --force flag to complete flash."
            )

        if trigged_copy:
            live_countdown(15.0, "Remote copy")

    # Soooo while this will make our problems go away by making the post reset flash more reliable we have to be really careful to not cause further problems on "complex"
    # topologies nb-nb or nb-galaxy, will have to wait for more comprehensive reset improvements before we can make use of it.
    can_reset = None
    if False:
        # During an m3 firmware update the initial firmware load may cause the board to not reinitialize after the initial reset. We can manually trigger the reset in such
        # a way that this error does not occur. However this will require a full board reset. In the case of multiple n300s/n150s conencted to eachother via ethernet we must
        # do this reset
        if boardname in ["NEBULA_X1", "NEBULA_X2"]:
            print("Checking if board can be automatically reset")
            can_reset = False

            try:
                can_reset = (
                    chip.m3_fw_app_version() >= (5, 5, 0, 0)
                    and chip.arc_l2_fw_version() >= (2, 0xC, 0, 0)
                    and chip.smbus_fw_version() >= (2, 0xC, 0, 0)
                )
                if can_reset:
                    print(
                        "Board can be reset; the reset will be triggered if the reset of the flashed chips healthy"
                    )
            except Exception as e:
                print(
                    "Board cannot be reset: Failed to get the current firmware versions"
                )
                error = True

            if can_reset:
                print("Performing full reset of the board to complete update.")
                try:
                    WHChipReset().full_lds_reset(
                        pci_interfaces=[chip.interface_id], reset_m3=True
                    )
                    print("Reset complete, now waiting for post flash verification.")
                except Exception as e:
                    can_reset = False

                try:
                    chip.reinit()
                    print("Reinitialized chip after post flash reset")
                except:
                    print("Failed to reinitialize chip after the post flash reset.")
                    can_reset = False
        elif boardname == "GALAXY":
            can_reset = False
        else:
            # Only need to asic+m3 reset nb boards and galaxy modules
            can_reset = None

    if can_reset is not None:
        if can_reset:
            print(
                "Flash complete, the new firmware is loaded and the board is ready for use"
            )
            return 0
        else:
            print(
                "Could not perform automatic chip reset, please reset/reboot manually. If the board does not reinitialize post reset then reboot one more time to complete the update."
            )
            return 1

    if boardname in ["NEBULA_X1", "NEBULA_X2", "GALAXY"]:
        print(
            "ATTENTION: The n150, n300 or galaxy module may fail to reinitialize on first reset post flash if this happens please reboot your system one more time."
        )

    if error:
        if isinstance(chip, GsChip):
            print(
                "Flash complete with error, powercycle the board (a host reboot will usually accomplish this) to reload the board and try to flash again."
            )
        else:
            print(
                "Flash complete with error, reset the board to reload the board and try to flash again."
            )
        return 1
    else:
        if isinstance(chip, GsChip):
            print(
                "Flash complete, powercycle the board (a host reboot will usually accomplish this) to load the new firmware."
            )
        else:
            print("Flash complete, reset the board to load the new firmware.")
        return 0
