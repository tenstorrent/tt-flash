# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import datetime
import os
import json
import sys
import tarfile

import tt_flash
from tt_flash import utility
from tt_flash.error import TTError
from tt_flash.version import extract_fw_versions
from tt_flash.flash import flash_chip

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

    subparsers = parser.add_subparsers(title="command", dest="command", required=True)

    flash = subparsers.add_parser("flash")
    flash.add_argument("--fw-tar", help="Path to the firmware tarball", required=True)
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

    # version = subparsers.add_parser("version")
    # version.add_argument("--fw-tar", help="Path to the firmware tarball", required=True)
    # version.add_argument(
    #     "--verbose",
    #     default=False,
    #     action="store_true",
    #     help="Add more detailed information to the output",
    # )

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
    return parser, parser.parse_args(args=cmd_args)


def main():
    parser, args = parse_args()

    try:
        tar = tarfile.open(args.fw_tar, "r")
    except Exception as e:
        print(f"Opening of {args.fw_tar} failed with - {e}\n\n---\n")
        parser.print_help()
        sys.exit(1)

    manifest_data = tar.extractfile("./manifest.json")
    if manifest_data is None:
        raise TTError(f"Could not find manifest in {args.fw_tar}")

    manifest = json.loads(manifest_data.read())
    version = manifest.get("version", None)
    if version is None:
        raise TTError(f"Could not find version in {args.fw_tar}/manifest.json")

    try:
        int_version = tuple(map(int, version.split(".")))
        if len(int_version) != 3:
            int_version = None
    except ValueError:
        int_version = None

    if int_version is None:
        raise TTError(f"Invalid version ({version}) in {args.fw_tar}/manifest.json")

    devices = detect_local_chips(ignore_ethernet=True)

    if args.command == "version":
        if int_version < (0, 2):
            raise TTError(
                f"The flash package ({args.fw_tar}) does not support recovery you need package with a (0, 2)+ format"
            )

        for dev in devices:
            fw_versions = extract_fw_versions(dev, tar)
    elif args.command == "flash":
        if int_version[0] > 1:
            raise TTError(
                f"Unsupported version ({version}) this flash program only supports flashing pre 2.0 packages"
            )

        rc = 0
        for dev in devices:
            # Get the board type, first we will try to get the board_type via pci.
            # This will fail in two cases
            # 1) the board is completely broken and we will need to
            # reflash the board id (hopefully we still have enough access to fix the issue)
            # 2) we are using something like jtag in which case we'll probably want to probe the board
            #
            # For now I think we can ignore both of these problems but the fix for 1) would be to allow the user
            # to specify a board_id to overwrite the primary one with. Which could be problematic... the fix
            # for 2) would be to read the board id back from the spi. The fix for 2) will come when I add
            # version readback support

            try:
                boardname = utility.get_board_type(dev.board_type(), from_type=True)
            except:
                boardname = None

            if boardname is None:
                raise TTError(f"Did not recognize board type for {dev}")

            print(f"\nNow checking device {dev}:\n")

            rc += flash_chip(
                dev, boardname, tar, args.force, skip_missing_fw=args.skip_missing_fw
            )

        return rc
    else:
        raise TTError(f"No handler for command {args.command}.")


if __name__ == "__main__":
    sys.exit(main())
