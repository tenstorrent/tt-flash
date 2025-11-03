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

from tt_umd import (
    TopologyDiscovery,
    TTDevice,
    create_remote_wormhole_tt_device,
    TelemetryTag,
    ARCH,
    wormhole,
    PCIDevice,
    SmBusArcTelemetryReader,
)

from tt_flash import utility
from tt_flash.error import TTError


@dataclass
class FwVersion:
    allow_exception: bool
    exception: Optional[Exception]
    running: Optional[tuple[int, int, int, int]]
    spi: Optional[tuple[int, int, int, int]]


def get_bundle_version_v1(chip: TTChip) -> FwVersion:
    """
    Get the currently running bundle version for gs and wh, using a legacy method

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
        elif isinstance(chip, GsChip):
            prefix = "grayskull"
        elif isinstance(chip, BhChip):
            prefix = "blackhole"
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
    def __init__(self, chip):
        self.use_umd = isinstance(chip, TTDevice)
        if self.use_umd:
            self.umd_device = chip
            self.interface_id = chip.get_pci_device().get_device_info().pci_device
        else:
            self.luwen_chip = chip
            self.interface_id = chip.pci_interface_id()

        self.fw_defines = init_fw_defines(self)
        self.telmetry_cache = None

    def reinit(self, callback=None):
        if self.use_umd:
            self.umd_device = TTDevice.create(self.interface_id)
            self.umd_device.init_tt_device()
            self.telmetry_cache = None
        else:
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

    def get_telemetry(self):
        if self.use_umd:
            # For UMD, return a telemetry-like object with the required fields
            arch = self.umd_device.get_arch()
            
            # Create the appropriate telemetry reader based on architecture
            if arch == ARCH.WORMHOLE_B0:
                telem_reader = SmBusArcTelemetryReader(self.umd_device)
            else:
                telem_reader = self.umd_device.get_arc_telemetry_reader()
            
            # Create a simple telemetry object with the fields we need
            class UMDTelemetry:
                def __init__(self, reader, arch):
                    self.reader = reader
                    self.arch = arch
                    # Initialize telemetry fields
                    self.m3_app_fw_version = None
                    self.arc1_fw_version = None
                    self.arc0_fw_version = None
                    self.asic_location = None
                    self.fw_bundle_version = None
                    
                def get_field(self, tag):
                    if self.reader.is_entry_available(tag):
                        return self.reader.read_entry(tag)
                    return None
            
            # Map telemetry fields based on architecture
            if arch == ARCH.WORMHOLE_B0:
                telem_obj = UMDTelemetry(telem_reader, arch)
                # Map wormhole-specific fields
                telem_obj.m3_app_fw_version = telem_obj.get_field(wormhole.TelemetryTag.M3_APP_FW_VERSION)
                telem_obj.arc1_fw_version = telem_obj.get_field(wormhole.TelemetryTag.ARC1_FW_VERSION)
                telem_obj.arc0_fw_version = telem_obj.get_field(wormhole.TelemetryTag.ARC0_FW_VERSION)
                telem_obj.asic_location = telem_obj.get_field(wormhole.TelemetryTag.ASIC_RO)
                telem_obj.fw_bundle_version = telem_obj.get_field(wormhole.TelemetryTag.FW_BUNDLE_VERSION)
            else:
                telem_obj = UMDTelemetry(telem_reader, arch)
                # Map universal fields
                telem_obj.asic_location = telem_obj.get_field(TelemetryTag.HARVESTING_STATE)
                telem_obj.fw_bundle_version = telem_obj.get_field(TelemetryTag.FLASH_BUNDLE_VERSION)
            
            self.telmetry_cache = telem_obj
            return self.telmetry_cache
        else:
            self.telmetry_cache = self.luwen_chip.get_telemetry()
            return self.telmetry_cache

    def get_telemetry_unchanged(self) -> Telemetry:
        if self.telmetry_cache is None:
            self.get_telemetry()

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
        if self.use_umd:
            return (self.umd_device.get_board_id() >> 36) & 0xFFFFF
        else:
            return self.luwen_chip.pci_board_type()

    def spi_write(self, addr: int, data: bytes):
        if self.use_umd:
            self.umd_device.spi_write(addr, data)
        else:
            self.luwen_chip.spi_write(addr, data)

    def spi_read(self, addr: int, size: int) -> bytes:
        if self.use_umd:
            data = bytearray(size)
            self.umd_device.spi_read(addr, data)
            return bytes(data)
        else:
            data = bytearray(size)
            self.luwen_chip.spi_read(addr, data)
            return bytes(data)

    def arc_msg(self, *args, **kwargs):
        if self.use_umd:
            # UMD arc_msg returns a vector where first element is the exit code and the following are the results.
            # To match the pyluwen format, we return [first result, exit code]
            result = self.umd_device.arc_msg(*args, **kwargs)
            return [result[1], result[0]]
        else:
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
            if self.use_umd:
                temp = self.umd_device.get_spi_fw_bundle_version()
            else:
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


class GsChip(TTChip):
    def min_fw_version(self):
        return 0x1050000

    def __repr__(self):
        return f"Grayskull[{self.interface_id}]"

    def get_bundle_version(self) -> FwVersion:
        return get_bundle_version_v1(self)


def detect_local_chips(
    ignore_ethernet: bool = False,
    use_umd: bool = False,
) -> list[Union[GsChip, WhChip, BhChip]]:
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
    if use_umd:
        pci_ids = PCIDevice.enumerate_devices()
        for pci_id in pci_ids:
            umd_device = TTDevice.create(pci_id)
            umd_device.init_tt_device()
            arch = umd_device.get_arch()
            if arch == ARCH.WORMHOLE_B0:
                output.append(WhChip(umd_device))
            elif arch == ARCH.BLACKHOLE:
                output.append(BhChip(umd_device))
            else:
                raise ValueError("Did not recognize board")
    else:
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
            elif device.as_bh() is not None:
                output.append(BhChip(device.as_bh()))
            else:
                raise ValueError("Did not recognize board")

    if not did_draw:
        print(f"\tDetected Chips: {chip_count}")

    return output


def detect_chips(local_only: bool = False, use_umd: bool = False) -> list[Union[GsChip, WhChip, BhChip]]:
    output = []
    if use_umd:
        cluster_descriptor = TopologyDiscovery.create_cluster_descriptor()
        # Note: This will go away soon, the discovery process will return chips.
        # Note that we have to create mmio chips first, since they are passed to the construction of the remote chips.
        chips_to_construct = cluster_descriptor.get_chips_local_first(cluster_descriptor.get_all_chips())
        chip_map = {}
        for chip in chips_to_construct:
            if cluster_descriptor.is_chip_mmio_capable(chip):
                pci_device_num = cluster_descriptor.get_chips_with_mmio()[chip]
                umd_device = TTDevice.create(pci_device_num)
                umd_device.init_tt_device()
                chip_map[chip] = len(output)
                
                # Create appropriate TTChip wrapper based on architecture
                arch = umd_device.get_arch()
                if arch == ARCH.WORMHOLE_B0:
                    output.append(WhChip(umd_device))
                elif arch == ARCH.BLACKHOLE:
                    output.append(BhChip(umd_device))
                else:
                    # For unsupported architectures, create a generic TTChip
                    output.append(TTChip(umd_device))
            else:
                closest_mmio = cluster_descriptor.get_closest_mmio_capable_chip(chip)
                umd_device = create_remote_wormhole_tt_device(output[chip_map[closest_mmio]].umd_device, cluster_descriptor, chip)
                umd_device.init_tt_device()
                chip_map[chip] = len(output)
                
                # Remote devices are typically Wormhole
                output.append(WhChip(umd_device))
    else:
        for device in luwen_detect_chips(local_only=local_only):
            if device.as_gs() is not None:
                output.append(GsChip(device.as_gs()))
            elif device.as_wh() is not None:
                output.append(WhChip(device.as_wh()))
            elif device.as_bh() is not None:
                output.append(BhChip(device.as_bh()))
            else:
                raise ValueError("Did not recognize board")

    return output
