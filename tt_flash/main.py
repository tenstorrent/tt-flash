from __future__ import annotations

import argparse
import datetime
from datetime import date
from dataclasses import dataclass
import os
import json
import math
import re
import signal
import struct
import sys
import tarfile
import time

from base64 import b16decode
from typing import Callable

from yaml import safe_load

from tt_flash import utility
from tt_flash.comms import i2c, spi
from tt_flash.error import TTError

from pyluwen import PciChip as Chip
from pyluwen import detect_chips

IS_PYINSTALLER_BIN = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')

# Make version available in --help
with utility.package_root_path() as path:
    VERSION_FILE = path.joinpath(".ignored/version.txt")
    if os.path.isfile(VERSION_FILE):
        VERSION_STR = (open(VERSION_FILE, 'r').read().strip())
        if __doc__ == None:
            __doc__ = "Version: %s" % VERSION_STR
        else:
            __doc__ = "Version: %s. %s" % (VERSION_STR, __doc__)
        VERSION_DATE = datetime.datetime.strptime(VERSION_STR[:10], "%Y-%m-%d").date()
        VERSION_HASH = int(VERSION_STR[-16:], 16)

# "mapping" is global variable for simplicity
# TODO: encapsulate this in a class so that each "chip" instance
# can have a different mapping
mapping = {}
arc_fw_defines = {}

def get_chip_data(chip, file):
    with utility.package_root_path() as path:
        if chip.as_wh() is not None:
            prefix = "wormhole"
        elif chip.as_gs() is not None:
            prefix = "grayskull"
        else:
            raise TTError("Only support flashing Wh or GS chips")
        return open(str(path.joinpath(f".ignored/{prefix}/{file}")))

def init_mapping(my_chip):
    global mapping

    mapping = safe_load(get_chip_data(my_chip, "spi-extrom.yaml"))

def init_fw_defines(chip):
    global arc_fw_defines

    arc_fw_defines = safe_load(get_chip_data(chip, "arc_fw_defines.yaml"))

def read_spi_reg(my_chip, read_mapping):
    addr = mapping[read_mapping]['Address']
    b = mapping[read_mapping]['ArraySize']
    with spi.Spi(my_chip, mapping) as schip:
        read = schip.read(addr, b)

    read_val = f"{int.from_bytes(bytes(read), byteorder='little'):0{b * 2}x}"

    return read_val

def spi_lock(my_chip, block_protect=8):
    # By default, with block_protect=8, locks first 2**(8-1) sectors
    # = 512 KB (= 4K/sector * 128 sectors) of SPI
    with spi.Spi(my_chip, mapping) as schip:
        schip.lock_spi(block_protect)


def spi_unlock(my_chip):
    with spi.Spi(my_chip, mapping) as schip:
        schip.lock_spi(0)


def spi_read(my_chip, addr, size):
    with spi.Spi(my_chip, mapping) as schip:
        return schip.read(addr, size)

@dataclass
class FwData:
    addr: int
    data: bytes

@dataclass
class FwPackage:
    phy_fw: FwData
    gs_fw: FwData
    watchdog_fw: FwData
    bootrom_fw: FwData
    smbus_fw: FwData

def program_fw(my_chip: Chip, fw_data: FwPackage):
    with spi.Spi(my_chip, mapping) as schip:
        # a. PCIe PHY FW
        schip.write_from_bin_bytes(fw_data.phy_fw.addr, fw_data.phy_fw.data)

        # b. GS FW
        schip.write_from_hex_bytes(fw_data.gs_fw.addr, fw_data.gs_fw.data)

        # c. WATCHDOG FW
        schip.write_from_hex_bytes(fw_data.watchdog_fw.addr, fw_data.watchdog_fw.data)

        # d. SPI BOOTROM FW
        schip.write_from_bin_bytes(fw_data.bootrom_fw.addr, fw_data.bootrom_fw.data)

        # e. SMBUS/I2C FW
        schip.write_from_hex_bytes(fw_data.smbus_fw.addr, fw_data.smbus_fw.data)

def float_as_uint32(num):
    return struct.unpack("I", struct.pack("f", num))[0]


def get_harvest_efuse(my_chip):
    row_harvest = my_chip.arc_msg("MSG_TYPE_ARC_GET_HARVESTING")[0]
    return row_harvest

def get_harvest_ovr(my_chip, board_type):
    if board_type == 0x7 or board_type == 0xA:
        # E75 and E300x2 should have 2 rows harvested, so
        # program row harvesting override if needed
        req_harvest_rows = 2
    else:
        req_harvest_rows = 0

    row_harvest = get_harvest_efuse(my_chip)

    # bits [19:10] = tensix row harvest, [9:0] = mem row harvest
    harvested = row_harvest & 0x3FF | ((row_harvest >> 10) & 0x3FF)
    harvested_count = bin(harvested).count('1')
    req_harvest_ovr_count = req_harvest_rows - harvested_count

    if req_harvest_ovr_count <= 0:
        return 0
    else:
        # Harvest the required number of rows starting from row 10 (bit 19)
        harvest_ovr = 0
        # Iterate rows 9, 8, 7, ..., 0
        for i in reversed(range(10)):
            if (harvested & (1 << i)) == 0:
                # Harvest this row if it isn't already harvested
                harvest_ovr |= (1 << i + 10)
                req_harvest_ovr_count -= 1
            if req_harvest_ovr_count <= 0:
                break
        return harvest_ovr

def increment_reprogrammed_count(my_chip):
    reprogrammed_count = int(read_spi_reg(my_chip, "REPROGRAMMED_COUNT"), 16)
    if reprogrammed_count == 0xFFFF:
        reprogrammed_count = 1
    else:
        reprogrammed_count += 1

    with spi.Spi(my_chip, mapping) as schip:
        schip.write_mapping("REPROGRAMMED_COUNT", reprogrammed_count)

def rmw_param(chip, data: bytearray, spi_addr: int, data_addr: int, len: int) -> bytearray:
    # Read the existing data
    existing_data = spi_read(chip, spi_addr, len)

    # Do the RMW
    data[data_addr:data_addr + len] = existing_data

    return data

def incr_param(chip, data: bytearray, spi_addr: int, data_addr: int, len: int) -> bytearray:
    # Read the existing data
    existing_data = spi_read(chip, spi_addr, len)

    try:
        data_bytes = (int.from_bytes(existing_data, "little") + 1).to_bytes(len, "little")
    except OverflowError:
        # If we overflow, just set it to 0
        data_bytes = (1).to_bytes(len, "little")

    # Do the RMW
    data[data_addr:data_addr + len] = data_bytes

    return data

def date_param(chip, data: bytearray, spi_addr: int, data_addr: int, len: int) -> bytearray:
    today = date.today()
    int_date = int(f"0x{today.strftime('%Y%m%d')}", 16) # Date in 0xYYYYMMDD

    # Do the RMW
    data[data_addr:data_addr + len] = int_date.to_bytes(len, "little")

    return data

TAG_HANDLERS: dict[str, Callable[[Chip, bytearray, int, int, int], bytearray]] = {
    "rmw": rmw_param,
    "incr": incr_param,
    "date": date_param
}

def handle_args(my_chip, fw_package: tarfile.TarFile, args):
    if IS_PYINSTALLER_BIN or args.external:
        try:
            my_chip.arc_msg(arc_fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=True, timeout=0.1)
        except TTError as err:
            pass
            # Ok to keep going if there's a timeout

        board_info = int(read_spi_reg(my_chip, "BOARD_INFO"), 16)
        board_type = (board_info >> 36) & 0xFFFFF
        if board_type not in (0x1, 0x3, 0x7, 0xA):
            utility.FATAL("This version of tt-flash does not support this board!")
        rom_patch = int(read_spi_reg(my_chip, "ROM_PATCH_NUM"), 16)
        print(f"ROM version is: {hex(rom_patch)}. tt-flash version is: {VERSION_DATE.strftime('0x%Y%m%d')}")
        if args.force:
            print("Forced ROM update requested. ROM will now be updated.")
        elif rom_patch >= int(VERSION_DATE.strftime("0x%Y%m%d"), 16) and rom_patch != 0xFFFFFFFF:
            print("ROM does not need to be updated.")
            try:
                my_chip.arc_msg(arc_fw_defines["MSG_TYPE_ARC_STATE3"],
                                wait_for_done=True,
                                timeout=0.1)
            except TTError as err:
                pass
                # Ok to keep going if there's a timeout
            return
        else:
            print("tt-flash version > ROM version. ROM will now be updated.")

        spi_unlock(my_chip)

        try:
            # 0x1: E300
            # 0x3: E300-105
            # 0x7: E75
            # 0x8: Nebula
            # 0xA: E300x2
            # 0xB: Galaxy

            if board_type == 0x1:
                boardname = "E300"
            elif board_type == 0x3:
                boardname = "E300_105"
            elif board_type == 0x7:
                boardname = "E75"
            elif board_type == 0xA:
                boardname = "E300_X2"
            else:
                raise TTError(f"This version of tt-flash does not have support for a board with boardtype {board_type}!")

            image = fw_package.extractfile(f"./{boardname}/image.bin")
            mask = fw_package.extractfile(f"./{boardname}/mask.json")
            if image is None and mask is None:
                raise TTError(f"Could not find flash data for {boardname} in tarfile")
            elif image is None:
                raise TTError(f"Could not find flash image for {boardname} in tarfile; expected to see {boardname}/image.bin")
            elif mask is None:
                raise TTError(f"Could not find param data for {boardname} in tarfile; expected to see {boardname}/mask.json")

            # First we verify that the format of mask is valid so we don't partially flash before discovering that the mask is invalid
            mask = json.loads(mask.read())

            # I expected to see a list of dicts, with the keys
            # "start", "end", "tag"
            param_handlers = []
            for v in mask:
                start = v.get("start", None)
                end = v.get("end", None)
                tag = v.get("tag", None)

                if (start is None or not isinstance(start, int)) or \
                    (end is None or not isinstance(end, int)) or \
                    (tag is None or not isinstance(tag, str)):
                    raise TTError(f"Invalid mask format for {boardname}; expected to see a list of dicts with keys 'start', 'end', 'tag'")

                if tag in TAG_HANDLERS:
                    param_handlers.append(((start, end), TAG_HANDLERS[tag]))
                else:
                    if len(TAG_HANDLERS) > 0:
                        pretty_tags = [f"'{x}'" for x in TAG_HANDLERS.keys()]
                        pretty_tags[-1] = f"or {pretty_tags[-1]}"
                        raise TTError(f"Invalid tag {tag} for {boardname}; expected to see one of {pretty_tags}")
                    else:
                        raise TTError(f"Invalid tag {tag} for {boardname}; there aren't any tags defined!")

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
                            data = handler(my_chip, data, start, start - curr_addr, end - start)
                        elif start >= curr_addr and start < curr_stop and end >= curr_stop:
                            raise TTError(f"A parameter write ({start}:{end}) splits a writeable region ({curr_addr}:{curr_stop}) in {boardname}! This is not supported.")

                    if not isinstance(data, bytes):
                        data = bytes(data)
                    writes.append((curr_addr, data))

                    curr_addr = curr_stop

            print("Programming chip")

            # Now we write the image
            with spi.Spi(my_chip, mapping) as schip:
                for addr, data in writes:
                    schip.write(addr, data)
        finally:
            spi_lock(my_chip)

            try:
                my_chip.arc_msg(arc_fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=True, timeout=0.1)
            except TTError as err:
                pass
                # Ok to keep going if there's a timeout
    elif args.read:
        # Put ARC FW to sleep
        try:
            my_chip.arc_msg(arc_fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=False, timeout=0.1)
        except:
            pass
        time.sleep(0.1)

        try:
            with spi.Spi(my_chip, mapping) as schip:
                schip.check_spi(2)
        finally:
            # Reawaken ARC FW
            my_chip.arc_msg(arc_fw_defines["MSG_TYPE_ARC_STATE0"], wait_for_done=False, timeout=0.1)
            time.sleep(0.1)

def main():
    # Install sigint handler
    # def signal_handler(sig, frame):
    #     print("Ctrl-C: this process should not be interrupted")

    # signal.signal(signal.SIGINT, signal_handler)

    # Parse arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        # version=VERSION_STR,
        version=""
    )
    parser.add_argument(
        '--interface',
        type=str,
        default='all',
        help=
        'For multi-card systems "all" (default) iterates through all cards. "pci:0", "pci:1", etc. acts on a single card'
    )
    parser.add_argument('--force', default=False, action="store_true", help='Force update the ROM')
    parser.add_argument('--fw-tar', default='ttfw.tar.gz', help='Path to the firmware tarball')
    if not IS_PYINSTALLER_BIN:
        parser.add_argument('--read', default=False, action="store_true", help='Prints a summary of the SPI contents')
        parser.add_argument('--configure', default=False, action="store_true", help='Flashes the spi')
        parser.add_argument('--fw-only', default=False, action="store_true", help='Flashes only the fw')
        parser.add_argument('--skip-voltage-change',
                            default=False,
                            action="store_true",
                            help='Skips voltage switching for SPI programming')
        parser.add_argument(
            '--external',
            action='store_true',
            help='Run the external version when T6PY_RELEASE=0. External is default when T6PY_RELEASE=1.')
    args = parser.parse_args()

    try:
        tar = tarfile.open(args.fw_tar, "r")
    except Exception as e:
        print( "Opening of {} failed with - {}\n\n---\n".format( args.fw_tar, e ) )
        parser.print_help()
        sys.exit(1)

    manifest_data = tar.extractfile("./manifest.json")
    if manifest_data is None:
        raise TTError(f"Could not find manifest in {args.fw_tar}")

    manifest = json.loads(manifest_data.read())
    version = manifest.get("version", None)
    if version is None:
        raise TTError(f"Could not find version in {args.fw_tar}/manifest.json")

    error = False
    try:
        int_version = tuple(map(int, version.split(".")))
        if len(int_version) != 3:
            error = True

        if int_version > (0, 1, 0):
            raise TTError(f"Unsupported version ({version}) this flash program only supports up to 0.1.0")
    except ValueError:
        error = True

    if error:
        raise TTError(f"Invalid version ({version}) in {args.fw_tar}/manifest.json")

    devices = detect_chips()

    for dev in devices:
        if dev.as_gs() is None:
            print("Came across non GS chip, skipping")
            continue
        init_mapping(dev)
        init_fw_defines(dev)

        print(f"\nNow checking device {dev}:\n")

        chip = HackChip(dev)

        handle_args(chip, tar, args)

@dataclass
class HackAxi:
    chip: HackChip
    mapping: dict

    def read32(self, addr: str) -> int:
        it = self.mapping
        for i in addr.split("."):
            it = it[i]
        return self.chip.axi_read32(it)

    def write32(self, addr: str, value: int):
        it = self.mapping
        for i in addr.split("."):
            it = it[i]
        self.chip.axi_write32(it, value)

class HackDict(dict):
    def __init__(self, offset, *args, **kwargs):
        self.offset = offset
        super().__init__(*args, **kwargs)

    def __getitem__(self, item):
        return super().__getitem__(item)["Address"] + self.offset

    def get(self, item):
        return super().__getitem__(item)

@dataclass
class HackChip:
    chip: Chip

    @property
    def AXI(self) -> HackAxi:
        if not hasattr(self, "_AXI"):
            self._AXI = HackAxi(self, {
                "ARC_CSM": HackDict(0x1FE80000, safe_load(get_chip_data(self.chip, "csm.yaml"))),
                "ARC_RESET": HackDict(0x1FF30000, safe_load(get_chip_data(self.chip, "reset.yaml"))),
                "ARC_EFUSE": HackDict(0x1FF40000, safe_load(get_chip_data(self.chip, "efuse.yaml"))),
                "ARC_SPI": HackDict(0x1FF70000, safe_load(get_chip_data(self.chip, "spi.yaml")))
            })
        return self._AXI

    def axi_read32(self, addr: int) -> int:
        return self.chip.axi_read32(addr)
        # return self.chip.as_gs().pci_axi_read32(addr)

    def axi_write32(self, addr: int, value: int):
        return self.chip.axi_write32(addr, value)
        # self.chip.as_gs().pci_axi_write32(addr, value)

    def arc_msg(self, *args, **kwargs):
        return self.chip.arc_msg(*args, **kwargs)

if __name__ == '__main__':
    main()
