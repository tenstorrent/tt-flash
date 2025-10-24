# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Optional

import argparse
import datetime
import os
import json
import sys
import tarfile
from pathlib import Path

import tt_flash
from tt_flash import utility
from tt_flash.error import TTError
from tt_flash.utility import CConfig
from tt_flash.flash import flash_chips

from .chip import detect_local_chips

# Make version available in --help
with utility.package_root_path() as path:
    VERSION_FILE = path.joinpath(".ignored/version.txt")
    if os.path.isfile(VERSION_FILE):
        VERSION_STR = open(VERSION_FILE, "r").read().strip()
        VERSION_DATE = datetime.datetime.strptime(VERSION_STR[:10], "%Y-%m-%d").date()
        VERSION_HASH = int(VERSION_STR[-16:], 16)
    else:
        VERSION_STR = tt_flash.__version__

    if __doc__ is None:
        __doc__ = f"Version: {VERSION_STR}"
    else:
        __doc__ = f"Version: {VERSION_STR}. {__doc__}"


class ArgumentParseError(Exception):
    pass


# A custom ArgumentParser which by default will raise an Exception on error
# instead of exiting the program.
EXIT_ON_ERROR = False


class NoExitArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        global EXIT_ON_ERROR

        if EXIT_ON_ERROR:
            self.print_help(sys.stderr)
            self.exit(2, "%s: error: %s\n" % (self.prog, message))
        else:
            raise ArgumentParseError(message)


def parse_args():
    # Parse arguments
    parser = NoExitArgumentParser(description=__doc__)
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=VERSION_STR,
    )
    parser.add_argument(
        "--no-color",
        help="Disable the colorful output",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--no-tty",
        help="Force disable the tty command output",
        default=False,
        action="store_true",
    )

    subparsers = parser.add_subparsers(title="command", dest="command", required=True)

    flash = subparsers.add_parser("flash", help="Flash firmware to Tenstorrent devices on the system. Run tt-flash flash -h for further command-specific help.")
    flash.add_argument(
        "fwbundle",
        nargs="?",
        help="Path to the firmware bundle",
        type=Path,
    )
    flash.add_argument("--fw-tar", help="Path to the firmware tarball (deprecated)", type=Path)
    flash.add_argument(
        "--skip-missing-fw",
        help="If the fw packages doesn't contain the fw for a detected board, continue flashing",
        default=False,
        action="store_true",
        required=False,
    )
    flash.add_argument(
        "--force", default=False, action="store_true", help="Force update the ROM"
    )
    flash.add_argument(
        "--no-reset",
        help="Do not reset devices at the end of flash",
        default=False,
        action="store_true",
    )

    verify = subparsers.add_parser(
        "verify",
        help="Verify the contents of the SPI.\nWill display the currently running and flashed bundle version of the fw and checksum the fw against either what was flashed previously according the the file system state, or a given fw bundle.\nIn the case where a fw bundle or flash record are not provided the program will search known locations that the flash record may have been written to and exit with an error if it cannot be found or read. Run tt-flash verify -h for further command-specific help.",
    )
    config_group = verify.add_mutually_exclusive_group()
    config_group.add_argument(
        "fwbundle",
        nargs="?",
        help="Path to the firmware bundle",
        type=Path,
    )
    config_group.add_argument("--fw-tar", help="Path to the firmware tarball (deprecated)", type=Path)
    verify.add_argument(
        "--skip-missing-fw",
        help="If the fw packages doesn't contain the fw for a detected board, continue flashing",
        default=False,
        action="store_true",
        required=False,
    )
    flash.add_argument(
        "--allow-major-downgrades", default=False, action="store_true", help="Allow major version downgrades"
    )

    cmd_args = sys.argv.copy()[1:]

    # So... I want to swap to having tt-flash respond to explicit subcommands
    # but to maintain backwards compatibility I had to make sure that the flash subcommand
    # would be assumed if none was given.

    # To start I have set the argument parser to no exit on failure
    try:
        # First try to parse the initial command line arguments
        parser.parse_args(args=cmd_args)

        # If it passes then we can continue as normal
    except ArgumentParseError:
        # But if it failed then insert flash into the first argument.
        # This is fine as long as flash remains a valid first argument.
        # This does not break -h or -v because they will have triggered the program
        # to exit during the initial parse_args call.
        cmd_args.insert(0, "flash")

        try:
            # Now try to parse the arguments after inserting the flash subcommand.
            parser.parse_args(args=cmd_args)
        except ArgumentParseError:
            # If we still fail then it's likely that we had a different problem than no
            # subcommand specified. So remove the inserted flash to make the error reflect
            # what the user entered.
            cmd_args = cmd_args[1:]

    # Reenable exit on failure (the default behaviour)
    global EXIT_ON_ERROR
    EXIT_ON_ERROR = True

    # Parse the args with the default behaviour
    args = parser.parse_args(args=cmd_args)

    # One of either args.fwbundle or args.fw_tar is required
    if args.fwbundle is not None and args.fw_tar is not None:
        parser.error("argument --fw-tar not allowed with positional fwbundle argument")
    if args.fwbundle is None and args.fw_tar is None:
        parser.error("one of the following arguments are required: fwbundle or --fw-tar")

    # --fw-tar is deprecated, warn if it's being used
    if args.fw_tar:
        print(f"{CConfig.COLOR.YELLOW}Warning: --fw-tar is deprecated, use positional argument instead: tt-flash {args.command} {args.fw_tar}{CConfig.COLOR.ENDC}")

    return parser, args

def load_manifest(path: str):
    tar = tarfile.open(path, "r")

    manifest_data = tar.extractfile("./manifest.json")
    if manifest_data is None:
        raise TTError(f"Could not find manifest in {path}")

    manifest = json.loads(manifest_data.read())
    version = manifest.get("version", None)
    if version is None:
        raise TTError(f"Could not find version in {path}/manifest.json")

    try:
        int_version = tuple(map(int, version.split(".")))
        if len(int_version) != 3:
            int_version = None
    except ValueError:
        int_version = None

    if int_version is None:
        raise TTError(f"Invalid version ({version}) in {path}/manifest.json")

    return tar, int_version


def main():
    parser, args = parse_args()

    CConfig.force_no_tty = args.no_tty
    CConfig.COLOR.use_color = not args.no_color
    fwbundle = args.fwbundle or args.fw_tar

    try:
        if args.command == "flash":
            print(f"{CConfig.COLOR.GREEN}Stage:{CConfig.COLOR.ENDC} SETUP")
            try:
                tar, version = load_manifest(fwbundle)
            except Exception as e:
                print(f"Opening of {fwbundle} failed with - {e}\n\n---\n")
                parser.print_help()
                sys.exit(1)

            print(f"{CConfig.COLOR.GREEN}Stage:{CConfig.COLOR.ENDC} DETECT")
            devices = detect_local_chips(ignore_ethernet=True)

            print(f"{CConfig.COLOR.GREEN}Stage:{CConfig.COLOR.ENDC} FLASH")

            return flash_chips(
                devices,
                tar,
                args.force,
                args.no_reset,
                version,
                args.allow_major_downgrades,
                skip_missing_fw=args.skip_missing_fw,
            )
        else:
            raise TTError(f"No handler for command {args.command}.")
    except Exception as e:
        print(f"{CConfig.COLOR.RED}Error: {e} {CConfig.COLOR.ENDC}")


if __name__ == "__main__":
    sys.exit(main())
