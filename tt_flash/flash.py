# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from base64 import b16decode
from datetime import date
from enum import Enum, auto
import json
import requests
import signal
import tarfile
import time
from typing import Callable, Optional, Union
import sys

import tt_flash
from tt_flash.blackhole import boot_fs_write
from tt_flash.chip import BhChip, TTChip, GsChip, WhChip, detect_chips
from tt_flash.error import TTError
from tt_flash.utility import change_to_public_name, get_board_type, CConfig

from tt_tools_common.reset_common.wh_reset import WHChipReset
from tt_tools_common.reset_common.bh_reset import BHChipReset
from tt_tools_common.reset_common.galaxy_reset import GalaxyReset


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


def live_countdown(wait_time: float, name: str, print_initial: bool = True):
    if print_initial:
        print(f"{name} started, will wait {wait_time} seconds for it to complete")

    # If True then we are running in an interactive environment
    if CConfig.is_tty():
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


@dataclass
class FlashData:
    write: bytes
    name: str
    idname: str


class FlashStageResultState(Enum):
    Ok = auto()
    NoFlash = auto()
    Err = auto()


@dataclass
class FlashStageResult:
    state: FlashStageResultState
    can_reset: bool
    msg: str
    data: Optional[FlashData]


def flash_chip_stage1(
    chip: TTChip,
    boardname: str,
    manifest: Manifest,
    fw_package: tarfile.TarFile,
    force: bool,
    skip_missing_fw: bool = False,
) -> FlashStageResult:
    """
    Check the chip and determine if it is a candidate to be flashed.

    The possible outcomes for this function are
    1. The chip is running old fw and can be flashed
    2. The chip is running fw too old to get the status from
        a. Force was used, so it will get flashed
        b. Force was not used, return an error and don't continue the flash process
    3. The chip is running up to date fw, so we don't flash it
    4. Force was used so we flash the fw no matter what
    """

    try:
        chip.arc_msg(
            chip.fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=True, timeout=0.1
        )
    except Exception as err:
        # Ok to keep going if there's a timeout
        pass

    fw_bundle_version = chip.get_bundle_version()

    if fw_bundle_version.exception is not None:
        if fw_bundle_version.allow_exception:
            # Very old gs/wh fw doesn't have support for getting the fw version at all
            # so it's safe to assume that we need to update
            if force:
                print(
                    f"\t\t\tHit error {fw_bundle_version.exception} while trying to determine running firmware. Falling back to assuming that it needs an update"
                )
            else:
                raise TTError(
                    f"Hit error {fw_bundle_version.exception} while trying to determine running firmware. If you know what you are doing you may still update by re-rerunning using the --force flag."
                )
        else:
            # BH must always successfully be able to return a fw_version
            raise TTError(
                f"Hit error {fw_bundle_version.exception} while trying to determine running firmware."
            )

    bundle_version = None
    if fw_bundle_version.running is None:
        # Certain old fw versions won't have the running_bundle_version populated.
        # In that case we can just assume that an upgrade is required.
        if force:
            print(
                "\t\t\tLooks like you are running a very old set of fw, assuming that it needs an update"
            )
        else:
            raise TTError(
                "Looks like you are running a very old set of fw, it's safe to assume that it needs an update but please update it using --force"
            )
        print(f"\t\t\tNow flashing tt-flash version: {manifest.bundle_version}")
    else:
        component = fw_bundle_version.running[0]
        if component != manifest.bundle_version[0]:
            if force:
                print(
                    f"\t\t\tFound unexpected bundle version ('{component}'), however you ran with force so we are barreling onwards"
                )
            else:
                raise TTError(
                    f"Bundle fwId ({manifest.bundle_version[0]}) does not match expected fwId ({component}); {manifest.bundle_version} != {fw_bundle_version.running}"
                )

        print(
            f"\t\t\tROM version is: {fw_bundle_version.running}. tt-flash version is: {manifest.bundle_version}"
        )

    detected_version = True
    if force:
        detected_version = False
        print("\t\t\tForced ROM update requested. ROM will now be updated.")
    # Best check is for if we have already flashed the desired fw (or newer fw) to spi
    elif fw_bundle_version.spi is not None:
        if fw_bundle_version.spi >= manifest.bundle_version:
            # Now that we know if the SPI is newer we should check to see if the problem is that we have flashed the correct FW, but are running something too old
            if fw_bundle_version.running is not None:
                if fw_bundle_version.running >= manifest.bundle_version:
                    print("\t\t\tROM does not need to be updated.")
                if fw_bundle_version.running < manifest.bundle_version:
                    print(
                        "\t\t\tROM does not need to be updated, while the chip is running old FW the SPI is up to date. You can load the new firmware after a reboot, or in the case of WH a reset. Or skip this check with --force."
                    )
            else:
                print(
                    "\t\t\tROM does not need to be updated, cannot detect the running FW version but the SPI is ahead of the firmware you are attempting to flash. You can load the newer firmware after a reboot, or in the case of WH a reset. Or skip this check with --force."
                )

            return FlashStageResult(
                state=FlashStageResultState.NoFlash, data=None, msg="", can_reset=False
            )
    # We did not see any spi versions returned... just go by running
    elif fw_bundle_version.running is not None:
        if fw_bundle_version.running >= manifest.bundle_version:
            print("\t\t\tROM does not need to be updated.")
            return FlashStageResult(
                state=FlashStageResultState.NoFlash, data=None, msg="", can_reset=False
            )
    else:
        detected_version = False
        print(
            "\t\t\tWas not able to fetch current firmware information, assuming that it needs an update"
        )

    if detected_version:
        print("\t\t\tFW bundle version > ROM version. ROM will now be updated.")

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
        if skip_missing_fw:
            print(
                f"\t\t\tCould not find flash data for {boardname_to_display} in tarfile"
            )
            return FlashStageResult(
                state=FlashStageResultState.NoFlash, data=None, msg="", can_reset=False
            )
        else:
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

    # Now we load the image and start replacing parameters
    image = image.read()

    if isinstance(chip, BhChip):
        writes = []

        curr_addr = 0
        for line in image.decode("utf-8").splitlines():
            line = line.strip()
            if line.startswith("@"):
                curr_addr = int(line.lstrip("@").strip())
            else:
                data = b16decode(line)
                curr_stop = curr_addr + len(data)
                if not isinstance(data, bytes):
                    data = bytes(data)
                writes.append((curr_addr, data))

                curr_addr = curr_stop

        writes.sort(key=lambda x: x[0])

        write = bytearray()
        last_addr = 0
        for addr, data in writes:
            write.extend([0xFF] * (addr - last_addr))
            write.extend(data)
            last_addr = addr + len(data)

        write = boot_fs_write(chip, boardname_to_display, mask, write)
    else:
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
                writes.append((curr_addr, data))

                curr_addr = curr_stop

        writes.sort(key=lambda x: x[0])

        write = bytearray()
        last_addr = 0
        for addr, data in writes:
            write.extend([0xFF] * (addr - last_addr))
            write.extend(data)
            last_addr = addr + len(data)

    if boardname in ["NEBULA_X1", "NEBULA_X2"]:
        print(
            "\t\t\tBoard will require reset to complete update, checking if an automatic reset is possible"
        )
        can_reset = False

        try:
            can_reset = (
                chip.m3_fw_app_version() >= (5, 5, 0, 0)
                and chip.arc_l2_fw_version() >= (2, 0xC, 0, 0)
                and chip.smbus_fw_version() >= (2, 0xC, 0, 0)
            )
            if can_reset:
                print(
                    f"\t\t\t\t{CConfig.COLOR.GREEN}Success:{CConfig.COLOR.ENDC} Board can be auto reset; will be triggered if the flash is successful"
                )
        except Exception as e:
            print(
                f"\t\t\t\t{CConfig.COLOR.YELLOW}Fail:{CConfig.COLOR.ENDC} Board cannot be auto reset: Failed to get the current firmware versions. This won't stop the flash, but will require manual reset"
            )
            can_reset = False
    elif isinstance(chip, BhChip):
        can_reset = True
    else:
        can_reset = False

    return FlashStageResult(
        state=FlashStageResultState.Ok,
        can_reset=can_reset,
        msg="",
        data=FlashData(write=write, name=boardname_to_display, idname=boardname),
    )


def flash_chip_stage2(
    chip: TTChip,
    data: FlashData,
) -> Optional[bool]:
    # Install sigint handler
    def signal_handler(sig, frame):
        print("Ctrl-C Caught: this process should not be interrupted")

    def perform_write(chip, write):
        original_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            chip.spi_write(0, write)
        finally:
            signal.signal(signal.SIGINT, original_sigint_handler)

    def perform_verify(chip, write) -> Optional[Union[int, int]]:
        original_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            base_data = chip.spi_read(0, len(write))

            if base_data != write:
                first_mismatch = None
                mismatch_count = 0
                for index, (a, b) in enumerate(zip(base_data, write)):
                    if a != b:
                        mismatch_count += 1
                        if first_mismatch is None:
                            first_mismatch = index
                return first_mismatch, mismatch_count
        finally:
            signal.signal(signal.SIGINT, original_sigint_handler)

        return None

    if CConfig.is_tty():
        print(
            "\t\t\tWriting new firmware... (this may take up to 1 minute)",
            end="",
            flush=True,
        )
    else:
        print("\t\t\tWriting new firmware... (this may take up to 1 minute)")

    perform_write(chip, data.write)

    if CConfig.is_tty():
        print("\r\033[K", end="")
    print(
        f"\t\t\tWriting new firmware... {CConfig.COLOR.GREEN}SUCCESS{CConfig.COLOR.ENDC}"
    )

    print(
        "\t\t\tVerifying flashed firmware... (this may also take up to 1 minute)",
        end="",
        flush=True,
    )
    if not CConfig.is_tty():
        print()

    verify_result = perform_verify(chip, data.write)
    if verify_result is not None:
        (first_mismatch, mismatch_count) = verify_result

        if CConfig.is_tty():
            print(f"\r\033[K", end="")
        print(
            f"\t\t\tIntial verification: {CConfig.COLOR.RED}failed{CConfig.COLOR.ENDC}"
        )
        print(f"\t\t\t\tFirst Mismatch at: {first_mismatch}")
        print(f"\t\t\t\tFound {mismatch_count} mismatches")

        if CConfig.is_tty():
            print(
                "\t\t\tAttempted to write firmware one more time... (this, again, may also take up to 1 minute)",
                end="",
                flush=True,
            )
        else:
            print(
                "\t\t\tAttempted to write firmware one more time... (this, again, may also take up to 1 minute)"
            )

        perform_write(chip, data.write)

        if CConfig.is_tty():
            print("\r\033[K", end="")
        print(
            f"\t\t\tAttempted to write firmware one more time... {CConfig.COLOR.GREEN}SUCCESS{CConfig.COLOR.ENDC}"
        )

        print(
            "\t\t\tVerifying second flash attempt... (this may also take up to 1 minute)",
            end="",
            flush=True,
        )
        if not CConfig.is_tty():
            print()

        verify_result = perform_verify(chip, data.write)
        if verify_result is not None:
            (first_mismatch, mismatch_count) = verify_result

            if CConfig.is_tty():
                print(f"\r\033[K", end="")
            print(
                f"\t\t\tSecond verification {CConfig.COLOR.RED}failed{CConfig.COLOR.ENDC}, please do not reset or poweroff the board and contact support for further assistance."
            )

            print(f"\t\t\t\tFirst Mismatch at: {first_mismatch}")
            print(f"\t\t\t\tFound {mismatch_count} mismatches")
            return None

    if CConfig.is_tty():
        print(f"\r\033[K", end="")
    print(
        f"\t\t\tFirmware verification... {CConfig.COLOR.GREEN}SUCCESS{CConfig.COLOR.ENDC}"
    )

    trigged_copy = False
    if data.idname == "NEBULA_X2":
        print("\t\t\tInitiating local to remote data copy")

        # There is a bug in m3 app version 5.8.0.1 where we can trigger a boot loop during the left to right copy.
        # In this condition we will disable the auto-reset before triggering the left to right copy.
        if chip.m3_fw_app_version() == (5, 8, 0, 1):
            print("Mitigating bootloop bug")
            triggered_reset_disable = False
            try:
                chip.arc_msg(
                    chip.fw_defines["MSG_UPDATE_M3_AUTO_RESET_TIMEOUT"], arg0=0
                )
                triggered_reset_disable = True
            except Exception as e:
                print(
                    f"\t\t\t{CConfig.COLOR.BLUE}NOTE:{CConfig.COLOR.ENDC} Failed to disable the m3 autoreset; please reboot/reset your system and flash again to initiate the left to right copy."
                )
                return None
            if triggered_reset_disable:
                live_countdown(1.0, "\t\t\tDisable m3 reset")

        try:
            chip.arc_msg(chip.fw_defines["MSG_TRIGGER_SPI_COPY_LtoR"])
            trigged_copy = True
        except Exception as e:
            print(
                f"\t\t\t{CConfig.COLOR.BLUE}NOTE:{CConfig.COLOR.ENDC} Failed to initiate left to right copy; please reset the host to reset the board and then rerun the flash with the --force flag to complete flash."
            )
            return None

    return trigged_copy


@dataclass
class Manifest:
    data: dict
    bundle_version: tuple[int, int, int, int]


def verify_package(fw_package: tarfile.TarFile):
    manifest_data = fw_package.extractfile("./manifest.json")
    if manifest_data is None:
        if CConfig.is_tty():
            # HACK(drosen): Would not have ended the last line with a '\n'
            print("\n")
        raise TTError(
            "Could not find manifest in fw package, please check that the correct one was used."
        )
    manifest = json.loads(manifest_data.read())

    manifest_bundle_version = manifest.get("bundle_version", {})

    new_bundle_version = (
        manifest_bundle_version.get("fwId", 0),
        manifest_bundle_version.get("releaseId", 0),
        manifest_bundle_version.get("patch", 0),
        manifest_bundle_version.get("debug", 0),
    )

    global __SEMANTIC_BUNDLE_VERSION
    __SEMANTIC_BUNDLE_VERSION = list(new_bundle_version)

    return Manifest(data=manifest, bundle_version=new_bundle_version)


def flash_chips(
    sys_config: Optional[dict],
    devices: list[TTChip],
    fw_package: tarfile.TarFile,
    force: bool,
    no_reset: bool,
    skip_missing_fw: bool = False,
):
    print(f"\t{CConfig.COLOR.GREEN}Sub Stage:{CConfig.COLOR.ENDC} VERIFY")
    if CConfig.is_tty():
        print("\t\tVerifying fw-package can be flashed", end="", flush=True)
    else:
        print("\t\tVerifying fw-package can be flashed")
    manifest = verify_package(fw_package)

    if CConfig.is_tty():
        print(
            f"\r\t\tVerifying fw-package can be flashed: {CConfig.COLOR.GREEN}complete{CConfig.COLOR.ENDC}"
        )
    else:
        print(
            f"\t\tVerifying fw-package can be flashed: {CConfig.COLOR.GREEN}complete{CConfig.COLOR.ENDC}"
        )

    to_flash = []
    for dev in devices:
        print(
            f"\t\tVerifying {CConfig.COLOR.BLUE}{dev}{CConfig.COLOR.ENDC} can be flashed"
        )
        try:
            boardname = get_board_type(dev.board_type(), from_type=True)
        except:
            boardname = None

        if boardname is None:
            raise TTError(f"Did not recognize board type for {dev}")

        to_flash.append(boardname)

    print(f"\t{CConfig.COLOR.GREEN}Stage:{CConfig.COLOR.ENDC} FLASH")

    flash_data = []
    flash_error = []
    needs_reset_wh = []
    needs_reset_bh = []
    for chip, boardname in zip(devices, to_flash):
        print(
            f"\t\t{CConfig.COLOR.GREEN}Sub Stage{CConfig.COLOR.ENDC} FLASH Step 1: {CConfig.COLOR.BLUE}{chip}{CConfig.COLOR.ENDC}"
        )
        result = flash_chip_stage1(
            chip,
            boardname,
            manifest,
            fw_package,
            force,
            skip_missing_fw=skip_missing_fw,
        )

        if result.state == FlashStageResultState.Err:
            flash_error.append(f"{chip}: {result.msg}")
        elif result.state == FlashStageResultState.Ok:
            flash_data.append((chip, result.data))
            if result.can_reset:
                if isinstance(chip, WhChip):
                    needs_reset_wh.append(chip.interface_id)
                elif isinstance(chip, BhChip):
                    needs_reset_bh.append(chip.interface_id)

    rc = 0

    triggered_copy = False
    for chip, data in flash_data:
        print(
            f"\t\t{CConfig.COLOR.GREEN}Sub Stage{CConfig.COLOR.ENDC} FLASH Step 2: {CConfig.COLOR.BLUE}{chip} {{{data.name}}}{CConfig.COLOR.ENDC}"
        )
        result = flash_chip_stage2(chip, data)
        if result is None:
            rc += 1
        else:
            triggered_copy |= result

    # If we flashed an X2 then we will wait for the copy to complete
    if triggered_copy:
        print(
            f"\t\tFlash and verification for all chips completed, will now wait for n300 remote copy to complete..."
        )
        live_countdown(15.0, "\t\tRemote copy", print_initial=False)

    if len(needs_reset_wh) > 0 or len(needs_reset_bh) > 0:
        print(f"{CConfig.COLOR.GREEN}Stage:{CConfig.COLOR.ENDC} RESET")

        if no_reset:
            if rc != 0:
                print(
                    f"\t\tErrors detected during flash, would not have reset even if --no-reset was not given..."
                )
            else:
                print(
                    f"\t\tWould have reset to force m3 recovery, but did not due to --no-reset"
                )
        else:
            if rc != 0:
                print(f"\t\tErrors detected during flash, skipping automatic reset...")
            else:
                # Reset boards if necessary
                mobo_dict_list = []
                if sys_config is not None:
                    for mobo_dict in sys_config.get("wh_mobo_reset", {}):
                        # Only add the mobos that have a name
                        if "mobo" in mobo_dict:
                            if (
                                "MOBO NAME" not in mobo_dict["mobo"]
                                and mobo_dict["mobo"] in mobos
                            ):
                                mobo_dict_list.append(mobo_dict)

                    if len(mobo_dict_list) > 0:
                        GalaxyReset().warm_reset_mobo(mobo_dict_list)
                        # The mobo reset will also reset all nb cards connected to the mobo.
                        # So we'll just remove them here to avoid setting them again
                        wh_link_pci_indices = sys_config["wh_link_reset"]["pci_index"]
                        for entry in mobo_dict_list:
                            if (
                                "nb_host_pci_idx" in entry.keys()
                                and entry["nb_host_pci_idx"]
                            ):
                                # remove the list of WH PCIe index's from the reset list
                                wh_link_pci_indices = list(
                                    set(wh_link_pci_indices)
                                    - set(entry["nb_host_pci_idx"])
                                )
                            sys_config["wh_link_reset"][
                                "pci_index"
                            ] = wh_link_pci_indices
                        needs_reset_wh = [
                            idx
                            for idx in sys_config["wh_link_reset"]["pci_index"]
                            if idx in needs_reset_wh
                        ]

                if len(needs_reset_wh) > 0:
                    WHChipReset().full_lds_reset(
                        pci_interfaces=needs_reset_wh, reset_m3=True
                    )

                if len(needs_reset_bh) > 0:
                    BHChipReset().full_lds_reset(
                        pci_interfaces=needs_reset_bh, reset_m3=True
                    )

                if len(needs_reset_wh) > 0 or len(needs_reset_bh) > 0:
                    detect_chips()

    if rc == 0:
        print(f"FLASH {CConfig.COLOR.GREEN}SUCCESS{CConfig.COLOR.ENDC}")
    else:
        print(f"FLASH {CConfig.COLOR.RED}FAILED{CConfig.COLOR.ENDC}")

    return rc
