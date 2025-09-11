# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import os

from typing import Callable, Type, TYPE_CHECKING

from base64 import b16decode

if TYPE_CHECKING:
    from tt_flash.chip import TTChip

try:
    from importlib.resources import files, as_file
except (ModuleNotFoundError, ImportError):
    from importlib_resources import files, as_file
import sys
from typing import Optional

from tt_tools_common.ui_common.themes import CMD_LINE_COLOR


# Returns the root path of the package, so we can access data files and such
def package_root_path():
    return as_file(files("tt_flash"))


# Get path of this script. 'frozen' means packaged with pyinstaller.
def application_path():
    if getattr(sys, "frozen", False):
        application_path = os.path.dirname(sys.executable)
    elif __file__:
        application_path = os.path.dirname(__file__)
    else:
        application_path = None
    return application_path


def get_board_type(board_id: int, from_type: bool = False) -> Optional[str]:
    """
    Get board type from board ID string.
    Ex:
        Board ID: AA-BBBBB-C-D-EE-FF-XXX
                   ^     ^ ^ ^  ^  ^   ^
                   |     | | |  |  |   +- XXX
                   |     | | |  |  +----- FF
                   |     | | |  +-------- EE
                   |     | | +----------- D
                   |     | +------------- C = Revision
                   |     +--------------- BBBBB = Unique Part Identifier (UPI)
                   +--------------------- AA
    """
    if from_type:
        upi = board_id
        rev = None
    else:
        upi = (board_id >> 36) & 0xFFFFF
        rev = (board_id >> 32) & 0xF

    if upi == 0x1:
        if rev is None:
            return None

        if rev == 0x2:
            return "E300_R2"
        elif rev in (0x3, 0x4):
            return "E300_R3"
        else:
            return None
    elif upi == 0x3:
        return "E300_105"
    elif upi == 0x7:
        return "E75"
    elif upi == 0x8:
        return "NEBULA_CB"
    elif upi == 0xA:
        return "E300_X2"
    elif upi == 0xB:
        return "GALAXY"
    elif upi == 0x14:
        return "NEBULA_X2"
    elif upi == 0x18:
        return "NEBULA_X1"
    elif upi == 0x35:
        return "WH_UBB"
    elif upi == 0x36:
        return "P100-1"
    elif upi == 0x40:
        return "P150A-1"
    elif upi == 0x41:
        return "P150B-1"
    elif upi == 0x42:
        return "P150C-1"
    elif upi == 0x43:
        return "P100A-1"
    elif upi == 0x44:
        return "P300B-1"
    elif upi == 0x45:
        return "P300A-1"
    elif upi == 0x46:
        return "P300C-1"
    elif upi == 0x47:
        return "GALAXY-1"
    else:
        return None


def change_to_public_name(codename: str) -> str:
    name_map = {
        "E300_105": "e150",
        "E300_X2": "e300",
        "E75": "e75",
        "NEBULA_X1": "n150",
        "NEBULA_X2": "n300",
        "WH_UBB": "Galaxy Wormhole",
        "P100-1": "p100",
        "P150A-1": "p150a",
        "P150B-1": "p150b",
        "P150C-1": "p150c",
        "P300": "p300",
        "P300A": "p300",
        "P300C": "p300",
        "GALAXY-1": "Galaxy Blackhole",
    }

    boardname = name_map.get(codename)
    if boardname is None:
        return codename
    else:
        return boardname


def semver_to_hex(semver: str):
    """Converts a semantic version string from format 10.15.1 to hex 0x0A0F0100"""
    major, minor, patch = semver.split(".")
    byte_array = bytearray([0, int(major), int(minor), int(patch)])
    return f"{int.from_bytes(byte_array, byteorder='big'):08x}"


def date_to_hex(date: int):
    """Converts a given date string from format YYYYMMDDHHMM to hex 0xYMDDHHMM"""
    year = int(date[0:4]) - 2020
    month = int(date[4:6])
    day = int(date[6:8])
    hour = int(date[8:10])
    minute = int(date[10:12])
    byte_array = bytearray([year * 16 + month, day, hour, minute])
    return f"{int.from_bytes(byte_array, byteorder='big'):08x}"


def hex_to_semver(hexsemver: int):
    """Converts a semantic version string from format 0x0A0F0100 to 10.15.1"""
    major = hexsemver >> 16 & 0xFF
    minor = hexsemver >> 8 & 0xFF
    patch = hexsemver >> 0 & 0xFF
    return f"{major}.{minor}.{patch}"


def hex_to_date(hexdate: int):
    """Converts a date given in hex from format 0xYMDDHHMM to string YYYY-MM-DD HH:MM"""
    year = (hexdate >> 28 & 0xF) + 2020
    month = hexdate >> 24 & 0xF
    day = hexdate >> 16 & 0xFF
    hour = hexdate >> 8 & 0xFF
    minute = hexdate & 0xFF

    return f"{year:04}-{month:02}-{day:02} {hour:02}:{minute:02}"


class ConfigurableCmdColor:
    def __init__(self, use_color: bool) -> None:
        self.use_color = use_color

    def __getattr__(self, k):
        if k == "use_color":
            return self.use_color
        elif self.use_color:
            return getattr(CMD_LINE_COLOR, k)
        else:
            return ""


class CmdLineConfig:
    def __init__(self, use_color: bool, force_no_tty: bool) -> None:
        self.COLOR = ConfigurableCmdColor(use_color)
        self.force_no_tty = force_no_tty

    def is_tty(self) -> bool:
        return (not self.force_no_tty) and sys.stdout.isatty()


CConfig = CmdLineConfig(True, False)
