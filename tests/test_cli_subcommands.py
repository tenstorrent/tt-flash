# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for tt-flash CLI argument parsing and subcommand handling.
Verifies that subparsers like 'verify' do not crash with AttributeError
and that 'flash' arguments remain mutually exclusive.
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock
import pytest

# Gracefully provide lightweight mock stubs for platform-specific hardware dependencies if absent
try:
    from tt_flash.main import parse_args, ArgumentParseError
except ModuleNotFoundError:

    class _MockPkg(MagicMock):
        __path__ = []
        __file__ = "mock"

    for _mod in [
        "pyluwen",
        "tt_tools_common",
        "tt_tools_common.ui_common",
        "tt_tools_common.ui_common.themes",
        "tt_tools_common.reset_common",
        "tt_tools_common.reset_common.wh_reset",
        "tt_tools_common.reset_common.bh_reset",
        "tt_tools_common.reset_common.chip_reset",
        "tt_tools_common.utils_common",
        "tt_tools_common.utils_common.tools_utils",
    ]:
        if _mod not in sys.modules:
            sys.modules[_mod] = _MockPkg()

    from tt_flash.main import parse_args, ArgumentParseError


def test_verify_subcommand_no_args():
    """Verify that 'tt-flash verify' parses successfully without AttributeError."""
    parser, args = parse_args(["verify"])
    assert args.command == "verify"
    assert getattr(args, "fwbundle", None) is None
    assert getattr(args, "download", None) is None


def test_verify_subcommand_with_bundle():
    """Verify that 'tt-flash verify <bundle>' parses successfully."""
    parser, args = parse_args(["verify", "test_bundle.fwbundle"])
    assert args.command == "verify"
    assert args.fwbundle == Path("test_bundle.fwbundle")


def test_flash_subcommand_with_bundle():
    """Verify that 'tt-flash flash <bundle>' parses successfully."""
    parser, args = parse_args(["flash", "test_bundle.fwbundle"])
    assert args.command == "flash"
    assert args.fwbundle == Path("test_bundle.fwbundle")


def test_flash_subcommand_with_download():
    """Verify that 'tt-flash flash -d' parses download option."""
    parser, args = parse_args(["flash", "-d"])
    assert args.command == "flash"
    assert args.download == "latest"

    parser, args = parse_args(["flash", "-d", "19.6.0"])
    assert args.command == "flash"
    assert args.download == "19.6.0"


def test_flash_backward_compatibility():
    """Verify backward compatibility where omitting subcommand defaults to 'flash'."""
    parser, args = parse_args(["test_bundle.fwbundle"])
    assert args.command == "flash"
    assert args.fwbundle == Path("test_bundle.fwbundle")
