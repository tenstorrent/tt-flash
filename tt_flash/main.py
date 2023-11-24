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
from typing import Callable, Optional

from yaml import safe_load

from tt_flash import utility
from tt_flash.comms import spi
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
fw_defines = {}

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
    board_type: int

    @property
    def AXI(self) -> HackAxi:
        if not hasattr(self, "_AXI"):
            self._AXI = HackAxi(self, {
                "ARC_RESET": HackDict(0x1FF30000, safe_load(get_chip_data(self.chip, "reset.yaml", False))),
                "ARC_SPI": HackDict(0x1FF70000, safe_load(get_chip_data(self.chip, "spi.yaml", False)))
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

def get_chip_data(chip, file, internal: bool):
    with utility.package_root_path() as path:
        if chip.as_wh() is not None:
            prefix = "wormhole"
        elif chip.as_gs() is not None:
            prefix = "grayskull"
        else:
            raise TTError("Only support flashing Wh or GS chips")
        if internal:
            prefix = f".ignored/{prefix}"
        else:
            prefix = f"data/{prefix}"
        return open(str(path.joinpath(f"{prefix}/{file}")))

def init_mapping(my_chip):
    global mapping

    mapping = safe_load(get_chip_data(my_chip, "spi-extrom.yaml", False))

def init_fw_defines(chip):
    global fw_defines

    fw_defines = safe_load(get_chip_data(chip, "fw_defines.yaml", False))

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
    with spi.Spi(my_chip, {}) as schip:
        schip.lock_spi(block_protect)

def spi_unlock(my_chip):
    with spi.Spi(my_chip, {}) as schip:
        schip.lock_spi(0)

def spi_read(my_chip, addr, size):
    with spi.Spi(my_chip, {}) as schip:
        return schip.read(addr, size)

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

def handle_args(my_chip: HackChip, fw_package: tarfile.TarFile, manifest_bundle_version: dict, args):
    if IS_PYINSTALLER_BIN or args.external:
        try:
            my_chip.arc_msg(fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=True, timeout=0.1)
        except Exception as err:
            # Ok to keep going if there's a timeout
            pass

        if my_chip.board_type not in (0x1, 0x3, 0x7, 0xA):
            utility.FATAL("This version of tt-flash does not support this board!")

        arc_bundle_version = 0xFFFFFFFF
        new_bundle_version = (manifest_bundle_version.get("fwId", 0), manifest_bundle_version.get("releaseId", 0), manifest_bundle_version.get("patch", 0), manifest_bundle_version.get("debug", 0))

        old_fw = False
        bundle_version = (0, 0, 0, 0)
        try:
            fw_version = my_chip.arc_msg(fw_defines["MSG_TYPE_FW_VERSION"], wait_for_done=True, arg0=0, arg1=0)[0]

            # Pre fw version 5 we don't have bundle support
            # this version of tt-flash only works with bundled fw
            # so it's safe to assume that we need to update
            if fw_version < 0x01050000:
                old_fw = True
            else:
                arc_bundle_version = my_chip.arc_msg(fw_defines["MSG_TYPE_FW_VERSION"], wait_for_done=True, arg0=2, arg1=0)[0]

                if arc_bundle_version == 0xDEAD:
                    old_fw = True
        except Exception:
            # Very old fw doesn't have support for getting the fw version at all
            # so it's safe to assume that we need to update
            old_fw = True

        if old_fw:
            if args.force:
                print("Looks like you are running a very old set of fw, assuming that it needs an update")
            else:
                raise TTError("Looks like you are running a very old set of fw, it's safe to assume that it needs an update but please update it using --force")

            print(f"Now flashing tt-flash version: {new_bundle_version}")
        else:
            patch = arc_bundle_version & 0xFF
            minor = (arc_bundle_version >> 8) & 0xFF
            major = (arc_bundle_version >> 16) & 0xFF
            component = (arc_bundle_version >> 24) & 0xFF
            bundle_version = (component, major, minor, patch)
            if component != new_bundle_version[0]:
                raise TTError(f"Bundle fwId ({new_bundle_version[0]}) does not match expected fwId ({component})")


            print(f"ROM version is: {bundle_version}. tt-flash version is: {new_bundle_version}")
        if args.force:
            print("Forced ROM update requested. ROM will now be updated.")
        elif bundle_version >= new_bundle_version and arc_bundle_version not in [0xFFFFFFFF, 0xDEAD]:
            print("ROM does not need to be updated.")
            try:
                my_chip.arc_msg(fw_defines["MSG_TYPE_ARC_STATE3"],
                                wait_for_done=True,
                                timeout=0.1)
            except TTError as err:
                # Ok to keep going if there's a timeout
                pass
            return
        else:
            print("tt-flash version > ROM version. ROM will now be updated.")

        # 0x1: E300
        # 0x3: E300-105
        # 0x7: E75
        # 0x8: Nebula
        # 0xA: E300x2
        # 0xB: Galaxy

        if my_chip.board_type == 0x1:
            boardname = "E300"
        elif my_chip.board_type == 0x3:
            boardname = "E300_105"
        elif my_chip.board_type == 0x7:
            boardname = "E75"
        elif my_chip.board_type == 0xA:
            boardname = "E300_X2"
        else:
            raise TTError(f"This version of tt-flash does not have support for a board with boardtype {my_chip.board_type}!")

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

        spi_unlock(my_chip)

        try:
            # Now we write the image
            with spi.Spi(my_chip, {}) as schip:
                for addr, data in writes:
                    schip.write(addr, data)
        finally:
            spi_lock(my_chip)

            try:
                my_chip.arc_msg(fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=True, timeout=0.1)
            except TTError as err:
                # Ok to keep going if there's a timeout
                pass
    # elif args.read:
    #     # Put ARC FW to sleep
    #     try:
    #         my_chip.arc_msg(fw_defines["MSG_TYPE_ARC_STATE3"], wait_for_done=False, timeout=0.1)
    #     except:
    #         pass
    #     time.sleep(0.1)

    #     try:
    #         with spi.Spi(my_chip, {}) as schip:
    #             schip.check_spi(2)
    #     finally:
    #         # Reawaken ARC FW
    #         my_chip.arc_msg(fw_defines["MSG_TYPE_ARC_STATE0"], wait_for_done=False, timeout=0.1)
    #         time.sleep(0.1)

def main():
    # Install sigint handler
    def signal_handler(sig, frame):
        print("Ctrl-C: this process should not be interrupted")

    signal.signal(signal.SIGINT, signal_handler)

    # Parse arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        # version=VERSION_STR,
        version=""
    )
    #parser.add_argument(
    #    '--interface',
    #    type=str,
    #    default='all',
    #    help=
    #    'For multi-card systems "all" (default) iterates through all cards. "pci:0", "pci:1", etc. acts on a single card'
    #)
    parser.add_argument('--force', default=False, action="store_true", help='Force update the ROM')
    parser.add_argument('--fw-tar', default='ttfw.tar.gz', help='Path to the firmware tarball')
    # if not IS_PYINSTALLER_BIN:
        # parser.add_argument('--read', default=False, action="store_true", help='Prints a summary of the SPI contents')
        # parser.add_argument('--configure', default=False, action="store_true", help='Flashes the spi')
        # parser.add_argument('--fw-only', default=False, action="store_true", help='Flashes only the fw')
    #parser.add_argument('--skip-voltage-change',
    #                    default=False,
    #                    action="store_true",
    #                    help='Skips voltage switching for SPI programming')
        # parser.add_argument(
        #     '--external',
        #     action='store_true',
        #     help='Run the external version when T6PY_RELEASE=0. External is default when T6PY_RELEASE=1.')
    args = parser.parse_args()

    args.external = True

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

        board_type = dev.as_gs().pci_board_type()

        # init_mapping(dev)
        init_fw_defines(dev)

        print(f"\nNow checking device {dev}:\n")

        chip = HackChip(dev, board_type)
        handle_args(chip, tar, manifest.get("bundle_version", {}), args)

if __name__ == '__main__':
    main()
