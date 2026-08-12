# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import time
from typing import Union, Optional
import sys
import yaml

from pyluwen import PciChip, Telemetry
from pyluwen import detect_chips as luwen_detect_chips
from pyluwen import detect_chips_fallible as luwen_detect_chips_fallible

from collections import defaultdict

from tt_flash import utility
from tt_flash.error import TTError
from tt_flash.utility import CConfig, get_board_type


@dataclass
class FwVersion:
    allow_exception: bool
    exception: Optional[Exception]
    running: Optional[tuple[int, int, int, int]]
    spi: Optional[tuple[int, int, int, int]]


def get_bundle_version_v1(chip: TTChip) -> FwVersion:
    """
    Get the currently running bundle version for wh, using a legacy method

    @param chip

    @return the detected fw bundle version.
    """
    running_bundle_version = None
    spi_bundle_version = None
    exception = None

    try:
        fw_version = chip.arc_msg(
            chip.fw_defines["MSG_TYPE_FW_VERSION"], wait_for_done=True, arg0=0, arg1=0
        )[0]

        # Pre fw version 5 we don't have bundle support
        # this version of tt-flash only works with bundled fw
        # so it's safe to assume that we need to update
        if fw_version >= chip.min_fw_version():
            temp = chip.arc_msg(
                chip.fw_defines["MSG_TYPE_FW_VERSION"],
                wait_for_done=True,
                arg0=1,
                arg1=0,
            )[0]

            if temp not in [0xFFFFFFFF, 0xDEAD]:
                patch = temp & 0xFF
                minor = (temp >> 8) & 0xFF
                major = (temp >> 16) & 0xFF
                component = (temp >> 24) & 0xFF
                running_bundle_version = (component, major, minor, patch)

            # There is a version of the firmware that doesn't correctly return an error when setting arg0 to an unknown option.
            # The running_bundle_version and fw_version can never be the same (as mandated by the version formatting) so I can safely check to see if they are the same when checking for this older FW.
            if (
                running_bundle_version != 0xDEAD
                and fw_version != running_bundle_version
            ):
                temp = chip.arc_msg(
                    chip.fw_defines["MSG_TYPE_FW_VERSION"],
                    wait_for_done=True,
                    arg0=2,
                    arg1=0,
                )[0]

                if temp not in [0xFFFFFFFF, 0xDEAD]:
                    patch = temp & 0xFF
                    minor = (temp >> 8) & 0xFF
                    major = (temp >> 16) & 0xFF
                    component = (temp >> 24) & 0xFF
                    spi_bundle_version = (component, major, minor, patch)
    except Exception as e:
        exception = e

    return FwVersion(
        allow_exception=True,
        exception=exception,
        running=running_bundle_version,
        spi=spi_bundle_version,
    )


def get_chip_data(chip, file, internal: bool):
    with utility.package_root_path() as path:
        if isinstance(chip, WhChip):
            prefix = "wormhole"
        elif isinstance(chip, BhChip):
            prefix = "blackhole"
        else:
            raise TTError("Only support flashing WH or BH chips")
        if internal:
            prefix = f".ignored/{prefix}"
        else:
            prefix = f"data/{prefix}"
        return open(str(path.joinpath(f"{prefix}/{file}")))


def init_fw_defines(chip):
    return yaml.safe_load(get_chip_data(chip, "fw_defines.yaml", False))


class TTChip:
    def __init__(self, chip: PciChip):
        self.luwen_chip = chip
        self.interface_id = chip.pci_interface_id()

        self.fw_defines = init_fw_defines(self)

        self.telmetry_cache = None
        self.board_id_cache = None

    def reinit(self, callback=None):
        self.luwen_chip = PciChip(self.interface_id)
        self.telmetry_cache = None
        self.board_id_cache = None

        chip_count = 0
        block_count = 0
        last_draw = time.time()

        def chip_detect_callback(status):
            nonlocal chip_count, last_draw, block_count

            if status.new_chip():
                chip_count += 1
            elif status.correct_down():
                chip_count -= 1
            chip_count = max(chip_count, 0)

            if sys.stdout.isatty():
                current_time = time.time()
                if current_time - last_draw > 0.1:
                    last_draw = current_time

                    if block_count > 0:
                        print(f"\033[{block_count}A", end="", flush=True)
                        print(f"\033[J", end="", flush=True)

                    print(f"\rDetected Chips: {chip_count}\n", end="", flush=True)
                    block_count = 1

                    status_string = status.status_string()
                    if status_string is not None:
                        for line in status_string.splitlines():
                            block_count += 1
                            print(f"\r{line}", flush=True)
            else:
                time.sleep(0.01)

        self.luwen_chip.init(
            callback=chip_detect_callback if callback is None else callback
        )

    def get_telemetry(self) -> Telemetry:
        self.telmetry_cache = self.luwen_chip.get_telemetry()
        return self.telmetry_cache

    def get_telemetry_unchanged(self) -> Telemetry:
        if self.telmetry_cache is None:
            self.telmetry_cache = self.luwen_chip.get_telemetry()

        return self.telmetry_cache

    def __vnum_to_version(self, version: int) -> tuple[int, int, int, int]:
        return (
            (version >> 24) & 0xFF,
            (version >> 16) & 0xFF,
            (version >> 8) & 0xFF,
            version & 0xFF,
        )

    def m3_fw_app_version(self):
        telem = self.get_telemetry_unchanged()
        return self.__vnum_to_version(telem.m3_app_fw_version)

    def smbus_fw_version(self):
        telem = self.get_telemetry_unchanged()
        return self.__vnum_to_version(telem.arc1_fw_version)

    def arc_l2_fw_version(self):
        telem = self.get_telemetry_unchanged()
        return self.__vnum_to_version(telem.arc0_fw_version)

    def get_asic_location(self) -> int:
        """
        Get the location of the ASIC on the chip for p300
        0 is L
        1 is R
        """
        telem = self.get_telemetry_unchanged()
        return telem.asic_location

    def board_type(self):
        return self.luwen_chip.pci_board_type()

    def board_id(self) -> int:
        # Opening the PCI device again is what makes this readable on a chip
        # whose firmware isn't answering, so the result is cached: callers ask
        # more than once per chip, and two reads that disagree would be worse
        # than one that is merely stale.
        if self.board_id_cache is None:
            self.board_id_cache = PciChip(self.interface_id).board_id()

        return self.board_id_cache

    def axi_write32(self, addr: int, value: int):
        self.luwen_chip.axi_write32(addr, value)

    def axi_write(self, addr: int, data: bytes):
        self.luwen_chip.axi_write(addr, data)

    def axi_read32(self, addr: int) -> int:
        return self.luwen_chip.axi_read32(addr)

    def axi_read(self, addr: int, size: int) -> bytes:
        data = bytearray(size)
        self.luwen_chip.axi_read(addr, data)

        return bytes(data)

    def spi_write(self, addr: int, data: bytes):
        self.luwen_chip.spi_write(addr, data)

    def spi_read(self, addr: int, size: int) -> bytes:
        data = bytearray(size)
        self.luwen_chip.spi_read(addr, data)

        return bytes(data)

    def arc_msg(self, *args, **kwargs):
        return self.luwen_chip.arc_msg(*args, **kwargs)

    @abstractmethod
    def min_fw_version(self):
        pass

    @abstractmethod
    def get_bundle_version(self) -> FwVersion:
        pass


class BhChip(TTChip):
    def min_fw_version(self):
        return 0x0

    def __repr__(self):
        return f"Blackhole[{self.interface_id}]"

    def get_bundle_version(self) -> FwVersion:
        running = None
        spi = None
        exception = None
        try:
            # Read running FW bundle version from telemetry
            telem = self.get_telemetry_unchanged()
            temp = telem.fw_bundle_version
            patch = temp & 0xFF
            minor = (temp >> 8) & 0xFF
            major = (temp >> 16) & 0xFF
            component = (temp >> 24) & 0xFF
            running = (component, major, minor, patch)

            # Read SPI FW bundle version
            cmfwcfg = self.luwen_chip.decode_boot_fs_table("cmfwcfg")
            temp = cmfwcfg["fw_bundle_version"]
            patch = temp & 0xFF
            minor = (temp >> 8) & 0xFF
            major = (temp >> 16) & 0xFF
            component = (temp >> 24) & 0xFF
            spi = (component, major, minor, patch)
        except Exception as e:
            exception = e

        return FwVersion(
            allow_exception=True, exception=exception, running=running, spi=spi
        )

    def get_asic_location(self) -> int:
        """
        Get the location of the ASIC on the chip for p300
        0 is L
        1 is R
        """
        # Records state of GPIO inputs [0:31] at boot time
        GPIO_STRAP_REG_L = 0x80030D20
        try:
            location = super().get_asic_location()
        except Exception:
            print(f"\rWarning: Unable to retrieve telemetry, reading ASIC location "
                "via fallback\n", end="", flush=True)
            gpio_strap = self.luwen_chip.axi_read32(GPIO_STRAP_REG_L)
            # If GPIO6 is high, we are on the left ASIC
            location = (gpio_strap >> 6) & 0x1

        return location


class WhChip(TTChip):
    def min_fw_version(self):
        return 0x2170000

    def __repr__(self):
        return f"Wormhole[{self.interface_id}]"

    def get_bundle_version(self) -> FwVersion:
        return get_bundle_version_v1(self)


def resolve_board_type(dev: Union[WhChip, BhChip]) -> Optional[str]:
    """
    Board type of a chip, or None if nothing on the chip identifies it.

    A chip running recovery firmware publishes no board id: board_id() either
    raises or reads back as 0, which get_board_type does not recognize. Ask the
    PCI device in that case. Its subsystem id carries the same UPI and does not
    depend on what the chip is running.
    """
    try:
        board_type = get_board_type(dev.board_id())
    except Exception:
        board_type = None

    if board_type is None:
        try:
            board_type = get_board_type(dev.board_type(), from_type=True)
        except Exception:
            board_type = None

    return board_type


def _asic_location(chip: BhChip) -> Optional[int]:
    """
    ASIC location of a chip, or None if it cannot be read.
    """
    try:
        return chip.get_asic_location()
    except Exception:
        return None


def _take_complement(pool: list[BhChip], chip: BhChip) -> Optional[BhChip]:
    """
    Remove and return the chip in pool that sits at the other ASIC location on
    a card, or None when nothing in pool complements chip.

    A P300 holds one ASIC at location 0 and one at location 1, so the only
    chip in the pool that can be chip's sibling is one reporting the other
    location. Any other choice pairs chips from different cards.
    """
    location = _asic_location(chip)
    if location is None:
        return None

    for i, candidate in enumerate(pool):
        if _asic_location(candidate) == 1 - location:
            return pool.pop(i)

    return None


def validate_p300_can_be_flashed(
    devices: list[Union[WhChip, BhChip]],
) -> tuple[list[Union[WhChip, BhChip]], bool]:
    """
    Validate that all detected P300 boards have both chips present. P300 boards without
    exactly 2 chips are excluded.

    Groups P300 chips by board_id (assuming unique board IDs per card in a production context)

    A chip running recovery firmware reports no board id and so cannot be
    grouped that way. It is still a chip that needs flashing -- that is how a
    board gets out of recovery -- so pair it with a group that is missing one,
    rather than leaving both halves of the card unflashed. Only a chip at the
    other ASIC location can be that sibling; see _take_complement.

    Also verifies that the P300 board has exactly 1 chip with asic_location = 0 and exactly
    1 chip with asic_location = 1

    Returns (filtered_devices, has_incomplete_p300)
    """
    p300_groups: dict[int, list[BhChip]] = defaultdict(list)
    unidentified: list[BhChip] = []
    valid_devices: list[Union[WhChip, BhChip]] = []

    for dev in devices:
        board_type = resolve_board_type(dev)

        if not (board_type and "P300" in board_type):
            # only checking p300 validity so add all other devices to valid_devices
            valid_devices.append(dev)
            continue

        try:
            board_id = dev.board_id()
        except Exception:
            board_id = 0

        if board_id:
            p300_groups[board_id].append(dev)
        else:
            unidentified.append(dev)

    boards: list[tuple[Optional[int], list[BhChip]]] = []
    for board_id, chips in p300_groups.items():
        if len(chips) == 1 and unidentified:
            sibling = _take_complement(unidentified, chips[0])
            if sibling is not None:
                chips.append(sibling)
        boards.append((board_id, chips))

    # Cards with neither chip able to name its board. The board id that would
    # group them is exactly what recovery firmware doesn't publish, so ASIC
    # location is all there is to pair on.
    while len(unidentified) >= 2:
        chip = unidentified.pop()
        sibling = _take_complement(unidentified, chip)
        if sibling is None:
            unidentified.append(chip)
            break
        boards.append((None, [chip, sibling]))
    if unidentified:
        boards.append((None, list(unidentified)))

    has_incomplete = False

    for board_id, chips in boards:
        board = f"board_id: {board_id:#x}" if board_id is not None else "no board id"

        # Doesn't have 2 chips
        if len(chips) != 2:
            has_incomplete = True
            print(
                CConfig.COLOR.RED,
                f"\tError: P300 board ({board}) has {len(chips)} chip(s) detected,",
                f"expected 2. Skipping flash for this board.",
                CConfig.COLOR.ENDC
            )
            continue

        # Has 2 chips, but verify that ASIC locations are expected
        try:
            locations = {c.get_asic_location() for c in chips}
        except Exception as e:
            has_incomplete = True
            print(
                CConfig.COLOR.RED,
                f"\tError: P300 board ({board}) has 2 chips but their ASIC ",
                f"locations could not be read ({e}). Skipping flash for this board.",
                CConfig.COLOR.ENDC
            )
            continue

        if locations == {0, 1}:
            valid_devices.extend(chips)
        else:
            has_incomplete = True
            print(
                CConfig.COLOR.RED,
                f"\tError: P300 board ({board}) has 2 chips but both report ",
                f"the same ASIC location. Skipping flash for this board.",
                CConfig.COLOR.ENDC
            )

    return valid_devices, has_incomplete


def detect_local_chips(
    ignore_ethernet: bool = False,
) -> list[Union[WhChip, BhChip]]:
    """
    This will create a chip which only guarantees that you have communication with the chip.
    """

    chip_count = 0
    block_count = 0
    last_draw = time.time()
    did_draw = False

    def chip_detect_callback(status):
        nonlocal chip_count, last_draw, block_count, did_draw

        if status.new_chip():
            chip_count += 1
        elif status.correct_down():
            chip_count -= 1
        chip_count = max(chip_count, 0)

        if sys.stdout.isatty():
            did_draw = True
            current_time = time.time()
            if current_time - last_draw > 0.1:
                last_draw = current_time

                if block_count > 0:
                    print(f"\033[{block_count}A", end="", flush=True)
                    print(f"\033[J", end="", flush=True)

                print(f"\rDetected Chips: {chip_count}\n", end="", flush=True)
                block_count = 1

                status_string = status.status_string()
                if status_string is not None:
                    for line in status_string.splitlines():
                        block_count += 1
                        print(f"\r{line}", flush=True)
        else:
            time.sleep(0.01)

    output = []
    for device in luwen_detect_chips_fallible(
        local_only=True,
        continue_on_failure=False,
        callback=chip_detect_callback,
        noc_safe=ignore_ethernet,
    ):
        if not device.have_comms():
            raise Exception(
                f"Do not have communication with {device}, you should reset or remove this device from your system before continuing."
            )

        device = device.force_upgrade()

        if device.as_wh() is not None:
            output.append(WhChip(device.as_wh()))
        elif device.as_bh() is not None:
            output.append(BhChip(device.as_bh()))
        else:
            raise ValueError("Did not recognize board")

    if not did_draw:
        print(f"\tDetected Chips: {chip_count}")

    return output


def detect_chips(local_only: bool = False) -> list[Union[WhChip, BhChip]]:
    output = []
    for device in luwen_detect_chips(local_only=local_only):
        if device.as_wh() is not None:
            output.append(WhChip(device.as_wh()))
        elif device.as_bh() is not None:
            output.append(BhChip(device.as_bh()))
        else:
            raise ValueError("Did not recognize board")

    return output
