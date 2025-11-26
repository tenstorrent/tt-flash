# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
import json
import requests
import signal
import tarfile
import time
from typing import Callable, Optional, Union
import sys
import random

import tt_flash
from tt_flash.blackhole import build_flash_writes_bh
from tt_flash.blackhole import FlashWrite
from tt_flash.chip import BhChip, TTChip, GsChip, WhChip, detect_chips
from tt_flash.error import TTError
from tt_flash.utility import (
    change_to_public_name,
    get_board_type,
    CConfig,
    live_countdown,
)
from tt_flash.wormhole import build_flash_writes_wh, set_semantic_bundle_version

from tt_tools_common.reset_common.wh_reset import WHChipReset
from tt_tools_common.reset_common.bh_reset import BHChipReset
from tt_tools_common.reset_common.galaxy_reset import GalaxyReset
from tt_tools_common.utils_common.tools_utils import detect_chips_with_callback
from pyluwen import run_wh_ubb_ipmi_reset, run_ubb_wait_for_driver_load

M3_DELAY_NORMAL = 20  # M3 takes 20s to boot and be ready after a reset
M3_DELAY_MAJOR_UPGRADE = (
    60  # When crossing major upgrade boundaries, it could take up to 60s
)

# Mapping of validation functions for each bundle version
BUNDLE_VALIDATION_FUNCS = {
    (2, 0, 0): lambda bundle_version: bundle_version[0]
    >= 19,  # Ensure major release is 19 or newer
}


@dataclass
class FlashData:
    write: list[FlashWrite]
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


@dataclass
class Manifest:
    data: dict
    bundle_version: tuple[int, int, int, int]


def normalize_fw_version(version: Optional[tuple[int, int, int, int]]) -> Optional[tuple[int, int, int, int]]:
    """
    Old FW bundles used to start with 80 and the version format was 80.major.minor.patch.
    FW version switched over at major version 18 from 80.18.X.X -> 18.X.X.
    
    If version[0] == 80, return (major, minor, patch, 0).
    Otherwise, just return the version.
    """
    if version is None:
        return None
    if version[0] == 80:
        return (version[1], version[2], version[3], 0)
    return version


def check_fw_version_compatability(
    chip: TTChip, manifest: Manifest, force: bool, allow_major_downgrades: bool
) -> tuple[bool, str]:
    """
    This function is used in Flash Stage 1 to perform the following:
    1. Read the fw_bundle_version from the chip. If any errors are encountered, only proceed if we detect that exceptions are allowed (e.g. in the case of very old GS or WH firmware)
    2. Check major version compatability between a chip's currently running FW and the FW we want to update to. Typically disallow major downgrades and only allow upgrades across one major version boundary.
    3. Check if the FW to flash is an upgrade, downgrade, or the same version. Skip the flash step if not an upgrade.

    All of the FW version checking functionality can be bypassed using --force.

    Returns (should_flash: bool, reason_message: str)
    """
    # Get current firmware bundle version from chip
    fw_bundle_version = chip.get_bundle_version()

    # If FW version is formatted like (80, major, minor, patch) reformat it to (major, minor, patch, 0)
    spi_version = normalize_fw_version(fw_bundle_version.spi)
    running_version = normalize_fw_version(fw_bundle_version.running)
    manifest_version = normalize_fw_version(manifest.bundle_version)

    # 1. Handle errors getting firmware version
    if fw_bundle_version.exception is not None:
        if fw_bundle_version.allow_exception:
            # Very old GS/WH FW doesn't have support for getting the FW version at all
            # so it's safe to assume that we need to update
            if not force:
                raise TTError(
                    f"Hit error {fw_bundle_version.exception} while trying to determine running firmware. "
                    "If you know what you are doing you may still update by rerunning using the --force flag."
                )
            print(
                f"\t\t\tHit error {fw_bundle_version.exception} while trying to determine running firmware. "
                "Falling back to assuming that it needs an update"
            )
        else:
            # BH must always successfully be able to return a fw_version
            raise TTError(
                f"Hit error {fw_bundle_version.exception} while trying to determine running firmware."
            )

    # 2. Check major version compatability
    # TODO: Why do we check running version and not SPI version here?
    if running_version is None:
        # Certain old fw versions won't have the running_bundle_version populated.
        # In that case we can just assume that an upgrade is required.
        if not force:
            raise TTError(
                "Looks like you are running a very old set of fw, it's safe to assume that it needs an update "
                "but please update it using --force"
            )
        print(
            "\t\t\tLooks like you are running a very old set of fw, assuming that it needs an update."
        )
    else:
        running_major = running_version[0]
        manifest_major = manifest_version[0]

        if running_major > manifest_major:
            if not allow_major_downgrades:
                raise TTError(
                    f"Detected major version downgrade from {running_version} to {manifest_version}, this is not supported. "
                    "If you really want to do this please re-run with --allow-major-downgrades"
                )
            print(
                f"\t\t\tDetected major version downgrade from {running_version} to {manifest_version}, "
                "but major downgrades are allowed so we are proceeding",
            )
        elif running_major == manifest_major - 1:
            # Permit updates across only one major version boundary
            print(
                f"{CConfig.COLOR.YELLOW}Detected major version upgrade from {running_version} to {manifest_version}{CConfig.COLOR.ENDC}",
            )
        elif running_major != manifest_major:
            if not force:
                raise TTError(
                    f"Bundle fwId ({manifest_major}) does not match expected fwId ({running_major}); "
                    f"{manifest_version} != {running_version} bypass with --force"
                )
            print(
                f"\t\t\tFound unexpected bundle version ('{running_major}'), however you ran with force so we are barreling onwards",
            )

        print(f"\t\t\tROM version is: {running_version}.")
    print(f"\t\t\ttt-flash version is: {manifest_version}")

    # 3. Verify that manifest_version is an upgrade, or continue using force.
    if force:
        print("\t\t\tForced ROM update requested.")
    elif spi_version is not None: 
        # The best way to check current FW version is SPI
        if spi_version >= manifest_version:
            # SPI already has desired version or newer, check for running_version mismatch
            if running_version is not None and running_version >= manifest_version:
                return (False, "ROM does not need to be updated.")
            elif running_version is not None:
                return (
                    False,
                    "ROM does not need to be updated, while the chip is running old FW the SPI is up to date. "
                    "You can load the new firmware after a reset. Or skip this check with --force.",
                )
            else:
                return (
                    False,
                    "ROM does not need to be updated, cannot detect the running FW version but the SPI is ahead of the firmware you are attempting to flash. "
                    "You can load the newer firmware after a reset. Or skip this check with --force.",
                )
    elif (
        running_version is not None
    ):  # The second best way to check current FW version is running_version
        if running_version >= manifest_version:
            return (False, "ROM does not need to be updated.")
    else:
        print(
            "\t\t\tWas not able to fetch current firmware information, assuming that it needs an update.",
        )
    # Passed all the checks, proceed with update
    print("\t\t\tROM will now be updated.")

    return (True, "")


def flash_chip_stage1(
    chip: TTChip,
    boardname: str,
    manifest: Manifest,
    fw_package: tarfile.TarFile,
    force: bool,
    allow_major_downgrades: bool,
    skip_missing_fw: bool = False,
) -> tuple[bool, str]:
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

    # Check if we can flash based on current version and version to flash
    should_flash, reason_message = check_fw_version_compatability(
        chip, manifest, force, allow_major_downgrades
    )
    if not should_flash:
        print("\t\t\t" + reason_message)
        return FlashStageResult(
            state=FlashStageResultState.NoFlash, data=None, msg="", can_reset=False
        )

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
        writes = build_flash_writes_bh(chip, image, mask, boardname_to_display)
    elif isinstance(chip, WhChip):
        writes = build_flash_writes_wh(chip, image, mask, boardname_to_display)
    else:
        if skip_missing_fw:
            print(
                f"\t\t\t{CConfig.COLOR.YELLOW}Skipping flash: Chip type not supported by TT-Flash.{CConfig.COLOR.ENDC}"
            )
            return FlashStageResult(
                state=FlashStageResultState.NoFlash, data=None, msg="", can_reset=False
            )
        else:
            raise TTError(f"Chip type not supported by TT-Flash")

    # Determine if board can be reset after flashing
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
    elif boardname == "WH_UBB":
        can_reset = True
    else:
        can_reset = False

    return FlashStageResult(
        state=FlashStageResultState.Ok,
        can_reset=can_reset,
        msg="",
        data=FlashData(write=writes, name=boardname_to_display, idname=boardname),
    )


def flash_chip_stage2(
    chip: TTChip,
    data: FlashData,
) -> Optional[bool]:
    # Install sigint handler
    def signal_handler(sig, frame):
        print("Ctrl-C Caught: this process should not be interrupted")

    def perform_write(chip, writes: FlashWrite):
        original_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            for write in writes:
                chip.spi_write(write.offset, write.write)
        finally:
            signal.signal(signal.SIGINT, original_sigint_handler)

    def perform_verify(chip, writes: FlashWrite) -> Optional[Union[int, int]]:
        original_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            for write in writes:
                base_data = chip.spi_read(write.offset, len(write.write))

                if base_data != write.write:
                    first_mismatch = None
                    mismatch_count = 0
                    for index, (a, b) in enumerate(zip(base_data, write.write)):
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


def verify_package(fw_package: tarfile.TarFile, version: tuple[int, int, int]):
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

    # Note- we only validate versions >= 2.0.0, for backwards compatibility with 1.x.x
    if version[0] != 1:
        if version not in BUNDLE_VALIDATION_FUNCS:
            raise TTError(
                f"Unsupported manifest version ({'.'.join(map(str, version))}). Please update tt-flash to the latest version."
            )
        elif not BUNDLE_VALIDATION_FUNCS[version](new_bundle_version):
            raise TTError(
                f"Bundle version {new_bundle_version} does not meet the requirements for version {'.'.join(map(str, version))}"
            )

    set_semantic_bundle_version(list(new_bundle_version))

    return Manifest(data=manifest, bundle_version=new_bundle_version)


def check_galaxy_eth_link_status(devices):
    """
    Check the Galaxy Ethernet link status.
    Returns True if the link is up, False otherwise.
    """
    noc_id = 0
    DEBUG_BUF_ADDR = 0x12C0  # For eth fw 5.0.0 and above
    eth_locations_noc_0 = [ (9, 0), (1, 0), (8, 0), (2, 0), (7, 0), (3, 0), (6, 0), (4, 0),
                        (9, 6), (1, 6), (8, 6), (2, 6), (7, 6), (3, 6), (6, 6), (4, 6) ]
    LINK_INACTIVE_FAIL_DUMMY_PACKET = 10
    # Check that we have 32 devices
    if len(devices) != 32:
        raise TTError(
            f"Expected 32 devices for Galaxy Ethernet link status check, seeing {len(devices)}, please try reset again or cold boot the system.",
        )

    # Collect all the link errors in a dictionary
    link_errors = {}
    # Check all 16 eth links for all devices
    for i, device in enumerate(devices):
        for eth in range(16):
            eth_x, eth_y = eth_locations_noc_0[eth]
            link_error = device.noc_read32(
                noc_id, eth_x, eth_y, DEBUG_BUF_ADDR + 0x4 * 96
            )
            if link_error == LINK_INACTIVE_FAIL_DUMMY_PACKET:
                link_errors[i] = eth

    if link_errors:
        for board_idx, eth in link_errors.items():
            print(
                CConfig.COLOR.RED,
                f"\t\tBoard {board_idx} has link error on eth port {eth}",
                CConfig.COLOR.ENDC,
            )
        raise TTError("Galaxy Ethernet link errors detected")


def glx_6u_trays_reset(
    reinit=True, ubb_num="0xF", dev_num="0xFF", op_mode="0x0", reset_time="0xF"
):
    """
    Reset the WH asics on the galaxy systems with the following steps:
    1. Reset the trays with ipmi command
    2. Wait for 30s
    3. Reinit all chips

    Args:
        reinit (bool): Whether to reinitialize the chips after reset.
        ubb_num (str): The UBB number to reset. 0x0~0xF (bit map)
        dev_num (str): The device number to reset. 0x0~0xFF(bit map)
        op_mode (str): The operation mode to use.
                        0x0 - Asserted/Deassert reset with a reset period (reset_time)
                        0x1 - Asserted reset
                        0x2 - Deasserted reset
        reset_time (str): The reset time to use. resolution 10ms (ex. 0xF => 15 => 150ms)
    """
    print(
        CConfig.COLOR.PURPLE,
        f"\t\tResetting Galaxy trays with reset command...",
        CConfig.COLOR.ENDC,
    )
    run_wh_ubb_ipmi_reset(ubb_num, dev_num, op_mode, reset_time)
    live_countdown(30, "Galaxy reset")
    run_ubb_wait_for_driver_load()
    print(
        CConfig.COLOR.PURPLE,
        f"\t\tRe-initializing boards after reset....",
        CConfig.COLOR.ENDC,
    )
    if not reinit:
        print(
            CConfig.COLOR.GREEN,
            f"\t\tExiting after galaxy reset without re-initializing chips.",
            CConfig.COLOR.ENDC,
        )
        return
    # eth status 2 has been reused to denote "connected", leading to false hangs when detecting chips
    # discover local only to fix that
    chips = detect_chips_with_callback(local_only=True, ignore_ethernet=True)
    # Check the eth link status for WH Galaxy

    # after re-init check eth status - only if doing a full galaxy reset.
    # If doing a partial reset, eth connections will be broken because eth training will go out of sync
    if ubb_num == 0xF:
        check_wh_galaxy_eth_link_status(chips)
    # All went well
    print(
        CConfig.COLOR.GREEN,
        f"\t\tRe-initialized {len(chips)} boards after reset.",
        CConfig.COLOR.ENDC,
    )


def flash_chips(
    devices: list[TTChip],
    fw_package: tarfile.TarFile,
    force: bool,
    no_reset: bool,
    version: tuple[int, int, int],
    allow_major_downgrades: bool,
    skip_missing_fw: bool = False,
):
    print(f"\t{CConfig.COLOR.GREEN}Sub Stage:{CConfig.COLOR.ENDC} VERIFY")
    if CConfig.is_tty():
        print("\t\tVerifying fw-package can be flashed ", end="", flush=True)
    else:
        print("\t\tVerifying fw-package can be flashed")
    manifest = verify_package(fw_package, version)

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

        # For p300 we need to check if its L or R chip
        if "P300" in boardname:
            # 0 = Right, 1 = Left
            if dev.get_asic_location() == 0:
                boardname = f"{boardname}_right"
            elif dev.get_asic_location() == 1:
                boardname = f"{boardname}_left"

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
            allow_major_downgrades,
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

        m3_delay = 20  # M3 takes 20 seconds to boot and be ready after a reset
        running_version = chip.get_bundle_version().running
        if (running_version is None) or (
            running_version[0] != manifest.bundle_version[0]
        ):
            # We crossed a major version boundary, give a longer boot timeout
            print(
                "\t\tDetected update across major version, will wait 60 seconds for m3 to boot after reset"
            )
            m3_delay = 60

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
                # All chips are on BH Galaxy UBB
                if set(to_flash) == {"GALAXY-1"}:
                    glx_6u_trays_reset()
                    # All BH chips have now been reset
                    # Don't reset them conventionally
                    needs_reset_bh = []

                # All chips are on WH Galaxy UBB
                elif set(to_flash) == {"WH_UBB"}:
                    glx_6u_trays_reset()
                    needs_reset_wh = [] # Don't reset WH chips conventionally

                if len(needs_reset_wh) > 0:
                    WHChipReset().full_lds_reset(
                        pci_interfaces=needs_reset_wh, reset_m3=True
                    )

                if len(needs_reset_bh) > 0:
                    BHChipReset().full_lds_reset(
                        pci_interfaces=needs_reset_bh, reset_m3=True, m3_delay=m3_delay
                    )

                if len(needs_reset_wh) > 0 or len(needs_reset_bh) > 0:
                    devices = detect_chips()

    for idx, chip in enumerate(devices):
        if manifest.bundle_version[0] >= 19 and isinstance(chip, BhChip):
            # Get a random number to send back as arg0
            check_val = random.randint(1, 0xFFFF)
            try:
                response = chip.arc_msg(
                    chip.fw_defines["MSG_CONFIRM_FLASHED_SPI"], arg0=check_val
                )
            except BaseException:
                response = [0]
            if (response[0] & 0xFFFF) != check_val:
                print(
                    f"{CConfig.COLOR.YELLOW}WARNING:{CConfig.COLOR.ENDC} Post flash check failed for chip {idx}"
                )
                print(
                    "Try resetting the board to ensure the new firmware is loaded correctly."
                )

    if rc == 0:
        print(f"FLASH {CConfig.COLOR.GREEN}SUCCESS{CConfig.COLOR.ENDC}")
    else:
        print(f"FLASH {CConfig.COLOR.RED}FAILED{CConfig.COLOR.ENDC}")

    return rc
