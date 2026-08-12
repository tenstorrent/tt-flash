# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tests for argument parsing of the verify subcommand (issue #106).

`-d/--download` is defined only on the flash subparser, but parse_args() and
main() both read args.download unconditionally, so every non-flash subcommand
raised AttributeError before dispatch was ever reached. The required-one-of
check had the same scope problem: verify is documented as falling back to the
flash record when no bundle is given, so requiring one rejected a documented
invocation.

No hardware required — nothing here reaches a device.

Usage:
    pytest tests/test_verify_command.py
"""

import sys

import pytest

import tt_flash.main as tt_main
from tt_flash.flash import VerifyResult
from tt_flash.main import main, parse_args


@pytest.fixture(autouse=True)
def reset_exit_on_error(monkeypatch):
    """Restore the module global parse_args() leaves flipped.

    parse_args() starts with EXIT_ON_ERROR False so the first parse can fail
    softly, then sets it True before returning. A second call in the same
    process therefore skips the "no subcommand implies flash" fallback. That
    never bites the CLI, which parses once per process, but it makes the
    function non-idempotent within a test session.
    """
    monkeypatch.setattr(tt_main, "EXIT_ON_ERROR", False)


@pytest.fixture
def argv(monkeypatch):
    """Set sys.argv for parse_args(), which reads it directly."""

    def _set(*args):
        monkeypatch.setattr(sys, "argv", ["tt-flash", *args])

    return _set


class TestVerifyParsing:
    """Issue #106 layer 1: AttributeError before dispatch."""

    def test_verify_with_no_bundle_parses(self, argv):
        """The documented no-argument form: falls back to the flash record."""
        argv("verify")
        _, args = parse_args()
        assert args.command == "verify"
        assert args.fwbundle is None

    def test_verify_with_bundle_parses(self, argv):
        argv("verify", "fw_pack-19.6.0.fwbundle")
        _, args = parse_args()
        assert args.command == "verify"
        assert str(args.fwbundle) == "fw_pack-19.6.0.fwbundle"

    def test_verify_has_no_download_attribute(self, argv):
        """--download belongs to flash only; reading it unguarded was the bug."""
        argv("verify")
        _, args = parse_args()
        assert not hasattr(args, "download")


class TestFlashParsingUnchanged:
    """Regression guards: the flash path must behave exactly as before."""

    def test_flash_with_no_arguments_still_errors(self, argv):
        argv("flash")
        with pytest.raises(SystemExit):
            parse_args()

    def test_flash_with_bundle_parses(self, argv):
        argv("flash", "fw_pack-19.6.0.fwbundle")
        _, args = parse_args()
        assert args.command == "flash"
        assert str(args.fwbundle) == "fw_pack-19.6.0.fwbundle"

    def test_flash_with_download_parses(self, argv):
        argv("flash", "--download")
        _, args = parse_args()
        assert args.command == "flash"
        assert args.download == "latest"

    def test_flash_with_download_version_parses(self, argv):
        argv("flash", "-d", "19.6.0")
        _, args = parse_args()
        assert args.download == "19.6.0"

    def test_bare_bundle_still_implies_flash(self, argv):
        """Backwards compatibility: no subcommand means flash."""
        argv("fw_pack-19.6.0.fwbundle")
        _, args = parse_args()
        assert args.command == "flash"


class TestVerifyDispatch:
    """Issue #106 layer 2: verify reaches a handler instead of falling through."""

    def test_verify_no_longer_falls_through_to_no_handler(self, argv, capsys):
        argv("--no-tty", "--no-color", "verify")
        rc = main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "No handler for command" not in out

    def test_verify_without_a_bundle_asks_for_one(self, argv, capsys):
        """The no-bundle form in --help depends on a flash record that this
        package never writes, so say so rather than failing obscurely."""
        argv("--no-tty", "--no-color", "verify")
        main()
        out = capsys.readouterr().out
        assert "verify needs a firmware bundle" in out
        assert "flash record" in out

    def test_verify_with_a_bundle_reaches_device_detection(
        self, argv, capsys, monkeypatch
    ):
        """With a readable bundle it gets as far as looking for hardware, which
        is where a machine with no Tenstorrent devices stops.

        The bundle itself is stubbed out: opening a real one is covered by the
        flash path, and what matters here is that verify orchestrates in the
        same order rather than falling through to no-handler.
        """
        monkeypatch.setattr(tt_main, "load_manifest", lambda path: (None, 0))
        monkeypatch.setattr(tt_main, "verify_package", lambda tar, version: None)
        monkeypatch.setattr(tt_main, "detect_local_chips", lambda **kwargs: [])
        argv("--no-tty", "--no-color", "verify", "fw_pack-19.6.0.fwbundle")
        with pytest.raises(SystemExit):
            main()
        out = capsys.readouterr().out
        assert "No devices available to verify" in out

    def test_verify_runs_each_detected_device(self, argv, capsys, monkeypatch):
        """Every detected device is checked, and a mismatch fails the run."""
        calls = []

        class FakeDevice:
            def __init__(self, interface_id):
                self.interface_id = interface_id

        def fake_verify_chip(interface_id, fwbundle, manifest, skip_missing_fw=False):
            calls.append(interface_id)
            return VerifyResult(
                debug_messages=[f"chip {interface_id} checked"],
                rc=1 if interface_id == 1 else 0,
            )

        monkeypatch.setattr(tt_main, "load_manifest", lambda path: (None, 0))
        monkeypatch.setattr(tt_main, "verify_package", lambda tar, version: None)
        monkeypatch.setattr(
            tt_main, "detect_local_chips", lambda **kwargs: [FakeDevice(0), FakeDevice(1)]
        )
        monkeypatch.setattr(tt_main, "verify_chip", fake_verify_chip)

        argv("--no-tty", "--no-color", "verify", "bundle.fwbundle")
        rc = main()

        assert calls == [0, 1]
        assert rc == 1
        assert "VERIFY" in capsys.readouterr().out
