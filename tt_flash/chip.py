# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from abc import ABC, abstractmethod
import time
from typing import Union
import sys
import yaml

from pyluwen import PciChip, Telemetry
from pyluwen import detect_chips as luwen_detect_chips
from pyluwen import detect_chips_fallible as luwen_detect_chips_fallible

from tt_flash import utility


def get_chip_data(chip, file, internal: bool):
    with utility.package_root_path() as path:
        if isinstance(chip, WhChip):
            prefix = "wormhole"
        elif isinstance(chip, GsChip):
            prefix = "grayskull"
        else:
            raise TTError("Only support flashing Wh or GS chips")
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

    def reinit(self, callback=None):
        self.luwen_chip = PciChip(self.interface_id)
        self.telmetry_cache = None

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
        return self.__vnum_to_version(telem.smbus_tx_m3_app_fw_version)

    def smbus_fw_version(self):
        telem = self.get_telemetry_unchanged()
        return self.__vnum_to_version(telem.smbus_tx_arc1_fw_version)

    def arc_l2_fw_version(self):
        telem = self.get_telemetry_unchanged()
        return self.__vnum_to_version(telem.smbus_tx_arc0_fw_version)

    def board_type(self):
        return self.luwen_chip.pci_board_type()

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


class WhChip(TTChip):
    def min_fw_version(self):
        return 0x2170000

    def __repr__(self):
        return f"Wormhole[{self.interface_id}]"


class GsChip(TTChip):
    def min_fw_version(self):
        return 0x1050000

    def __repr__(self):
        return f"Grayskull[{self.interface_id}]"


def detect_local_chips(ignore_ethernet: bool = False) -> list[Union[GsChip, WhChip]]:
    """
    This will create a chip which only gaurentees that you have communication with the chip.
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

        if device.as_gs() is not None:
            output.append(GsChip(device.as_gs()))
        elif device.as_wh() is not None:
            output.append(WhChip(device.as_wh()))
        else:
            raise ValueError("Did not recognize board")

    if not did_draw:
        print(f"\tDetected Chips: {chip_count}")

    return output


def detect_chips(local_only: bool = False) -> list[Union[GsChip, WhChip]]:
    output = []
    for device in luwen_detect_chips(local_only=local_only):
        if device.as_gs() is not None:
            output.append(GsChip(device.as_gs()))
        elif device.as_wh() is not None:
            output.append(WhChip(device.as_wh()))
        else:
            raise ValueError("Did not recognize board")

    return output
