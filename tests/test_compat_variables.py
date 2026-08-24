# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tests for board variable evaluation. No hardware required.

Usage:
    pytest test_compat_variables.py
"""

import io
import json
import tarfile

import pytest

from tt_flash import compat_variables
from tt_flash.compat_variables import CheckState, CompatVariables, describe_missing
from tt_flash.error import TTError

# TAG_FLASH_JEDEC_ID
JEDEC_TAG = 80

MT25 = 0x20BB20
GD25 = 0xC8631A


class FakeChip:
    """A chip that reports the telemetry tags it was given."""

    def __init__(self, telemetry: dict = None):
        self.telemetry = telemetry if telemetry is not None else {}

    def read_telemetry_tag(self, tag: int):
        return self.telemetry.get(tag)


def spi_eeprom(constraints: list, **overrides) -> dict:
    variable = {
        "name": "SPI EEPROM",
        "number": 0,
        "formatter": "hex",
        "source": {"type": "telemetry", "tag": JEDEC_TAG},
        "constraints": constraints,
    }
    variable.update(overrides)
    return variable


def compat(*variables, version: int = 1) -> CompatVariables:
    return CompatVariables.loads(
        json.dumps({"version": version, "variables": list(variables)})
    )


def test_constraint_satisfied():
    verified, checks = compat_variables.evaluate(
        FakeChip({JEDEC_TAG: GD25}),
        compat(spi_eeprom([{"in": ["0x20bb20", "0xc8631a"]}])),
    )

    assert verified == 1
    assert compat_variables.failed(checks) == []
    assert [c.state for c in checks] == [CheckState.PASSED]
    assert checks[0].message == ("SPI EEPROM is 0xc8631a, which this firmware supports")


def test_constraint_violated():
    verified, checks = compat_variables.evaluate(
        FakeChip({JEDEC_TAG: GD25}), compat(spi_eeprom([{"eq": "0x20bb20"}]))
    )

    assert verified == 0
    failures = compat_variables.failed(checks)
    assert len(failures) == 1
    assert failures[0].variable.number == 0
    assert failures[0].value == GD25
    assert failures[0].message == (
        "SPI EEPROM is 0xc8631a; this firmware supports 0x20bb20"
    )


@pytest.mark.parametrize(
    "constraints,value,expected",
    [
        ([{"eq": 4}], 4, True),
        ([{"ne": 4}], 4, False),
        ([{"lt": 4}], 3, True),
        ([{"lt": 4}], 4, False),
        ([{"le": 4}], 4, True),
        ([{"gt": 4}], 5, True),
        ([{"ge": 4}], 4, True),
        ([{"in": [1, 2]}], 2, True),
        ([{"not_in": [1, 2]}], 3, True),
        # Every entry must hold
        ([{"ge": 2}, {"lt": 4}], 3, True),
        ([{"ge": 2}, {"lt": 4}], 4, False),
        # A list can repeat an operator, which a map could not
        ([{"ne": 1}, {"ne": 2}], 3, True),
        ([{"ne": 1}, {"ne": 2}], 2, False),
        # An empty list permits anything
        ([], 99, True),
    ],
)
def test_operators(constraints, value, expected):
    verified, _ = compat_variables.evaluate(
        FakeChip({JEDEC_TAG: value}), compat(spi_eeprom(constraints))
    )

    assert bool(verified & 1) is expected


def test_values_may_be_decimal_or_hex():
    verified, _ = compat_variables.evaluate(
        FakeChip({JEDEC_TAG: MT25}), compat(spi_eeprom([{"eq": MT25}]))
    )

    assert verified == 1


def test_unreadable_variable_is_unverified():
    """
    A board that does not report the tag leaves the variable unverified, and
    says so rather than passing in silence.
    """
    verified, checks = compat_variables.evaluate(
        FakeChip(), compat(spi_eeprom([{"eq": "0x20bb20"}]))
    )

    assert verified == 0
    assert compat_variables.failed(checks) == []
    assert [c.state for c in checks] == [CheckState.UNCHECKED]
    assert checks[0].message == (
        "SPI EEPROM could not be read from this board, so it was not checked"
    )


def test_unknown_source_is_unverified():
    """Guessing at a retrieval method we don't know would defeat the check."""
    verified, checks = compat_variables.evaluate(
        FakeChip({JEDEC_TAG: MT25}),
        compat(spi_eeprom([{"eq": "0x20bb20"}], source={"type": "dmc_message"})),
    )

    assert verified == 0
    assert [c.state for c in checks] == [CheckState.UNCHECKED]


def test_unknown_operator_is_unverified():
    parsed = compat(spi_eeprom([{"between": [1, 2]}]))
    verified, checks = compat_variables.evaluate(FakeChip({JEDEC_TAG: MT25}), parsed)

    assert verified == 0
    assert [c.state for c in checks] == [CheckState.UNCHECKED]
    assert checks[0].message == (
        "SPI EEPROM is constrained in a way this tt-flash does not understand, "
        "so it was not checked"
    )
    assert parsed.unsupported


def test_unknown_format_version_verifies_nothing():
    """A format we don't know must not stop us flashing a board that needs none
    of it, but must not let us claim we checked anything either."""
    parsed = compat(spi_eeprom([{"eq": "0x20bb20"}]), version=99)
    verified, checks = compat_variables.evaluate(FakeChip({JEDEC_TAG: MT25}), parsed)

    assert verified == 0
    assert checks == []
    assert parsed.unsupported


def test_malformed_value_is_an_error():
    with pytest.raises(TTError):
        compat(spi_eeprom([{"eq": "not a number"}]))


@pytest.mark.parametrize(
    "formatter,expected",
    [
        ("hex", "0x1020304"),
        ("semver", "1.2.3"),
        # An unknown or absent formatter falls back to hex, so a newer bundle
        # never stops an older tt-flash from explaining itself
        ("morse", "0x1020304"),
        (None, "0x1020304"),
    ],
)
def test_formatters(formatter, expected):
    _, checks = compat_variables.evaluate(
        FakeChip({JEDEC_TAG: 0x01020304}),
        compat(spi_eeprom([{"eq": 0}], formatter=formatter)),
    )

    assert expected in checks[0].message


def test_describe_constraint():
    variable = compat(spi_eeprom([{"in": ["0x20bb20", "0xc8631a"]}])).variables[0]
    assert variable.describe_constraint() == "one of 0x20bb20, 0xc8631a"

    variable = compat(spi_eeprom([{"ge": 4}])).variables[0]
    assert variable.describe_constraint() == "ge 0x4"


def test_describe_missing_names_the_variable():
    parsed = compat(spi_eeprom([{"eq": "0x20bb20"}]))
    message = describe_missing(required=1, verified=0, compat=parsed)

    assert "SPI EEPROM" in message


def test_describe_missing_without_compat_variables():
    """A bundle that predates board variables can't name them, so say so."""
    message = describe_missing(required=1, verified=0, compat=None)

    assert "board variable 0" in message
    assert "newer bundle" in message


def make_bundle(board: str, compat_variables_json=None) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if compat_variables_json is not None:
            data = json.dumps(compat_variables_json).encode("utf-8")
            info = tarfile.TarInfo(f"./{board}/compat-variables.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


def test_load_from_bundle():
    bundle = make_bundle(
        "P150A-1", {"version": 1, "variables": [spi_eeprom([{"eq": "0x20bb20"}])]}
    )

    parsed = compat_variables.load(bundle, "P150A-1")

    assert parsed is not None
    assert parsed.variables[0].name == "SPI EEPROM"


def test_load_from_bundle_without_compat_variables():
    """Bundles built before board variables existed declare nothing."""
    parsed = compat_variables.load(make_bundle("P150A-1"), "P150A-1")

    assert parsed is None

    verified, checks = compat_variables.evaluate(FakeChip({JEDEC_TAG: GD25}), parsed)
    assert verified == 0
    assert checks == []


def parse_flash_args(argv: list[str]):
    """Parse a `tt-flash flash` command line."""
    import sys

    from tt_flash import main as tt_flash_main

    argv_backup = sys.argv
    sys.argv = ["tt-flash", "flash", "bundle.fwbundle"] + argv
    try:
        _, args = tt_flash_main.parse_args()
    finally:
        sys.argv = argv_backup
    return args


def test_force_does_not_bypass_variable_checks():
    """
    --force bypasses checks whose cost is a wasted flash. A board variable
    mismatch costs the board, so it needs its own opt-in.
    """
    args = parse_flash_args(["--force"])

    assert args.force
    assert not args.force_all_variable_checks


def test_force_all_variable_checks():
    args = parse_flash_args(["--force-all-variable-checks"])

    assert args.force_all_variable_checks
    assert not args.force


def test_force_all_variable_checks_defaults_off():
    assert not parse_flash_args([]).force_all_variable_checks


def test_force_all_variable_checks_is_hidden():
    """It should not be advertised to someone reading --help."""
    import io
    import sys

    from tt_flash import main as tt_flash_main

    argv_backup = sys.argv
    sys.argv = ["tt-flash", "flash", "bundle.fwbundle"]
    try:
        parser, _ = tt_flash_main.parse_args()
    finally:
        sys.argv = argv_backup

    flash_parser = parser._subparsers._group_actions[0].choices["flash"]
    help_text = io.StringIO()
    flash_parser.print_help(help_text)

    assert "--force-all-variable-checks" not in help_text.getvalue()
    # ... though the parser really does accept it, and --force is still shown
    assert "--force" in help_text.getvalue()
    assert parse_flash_args(["--force-all-variable-checks"]).force_all_variable_checks


# --- Driving flash_chip_stage1 with a real bundle -------------------------
#
# The checks above prove evaluate() reports a mismatch. These prove the
# report survives the trip out of flash_chip_stage1 to the caller, which is
# the part that decides whether anything is written.

BOARDNAME = "P150A-1"

# Two independent pointers into CSM, as the telemetry table publishes them
SCRATCH_RAM = {12: 0x8003_0030, 13: 0x8003_0034}
DATA_ADDR = 0x1000_1000
TABLE_ADDR = 0x1000_2000


class FakeLuwenChip:
    """A Blackhole reporting one telemetry tag, and answering ARC messages."""

    def __init__(self, jedec_id: int, unlock_response: list = None):
        # What firmware answers FLASH_UNLOCK with; the default accepts it
        self.unlock_response = unlock_response or [0] * 8
        self.memory = {
            SCRATCH_RAM[13]: TABLE_ADDR,
            SCRATCH_RAM[12]: DATA_ADDR,
            TABLE_ADDR: 1,
            TABLE_ADDR + 4: 1,
            TABLE_ADDR + 8: (0 << 16) | JEDEC_TAG,
            DATA_ADDR: jedec_id,
        }
        self.messages = []
        self.writes = []
        # Messages and writes in the order they arrived, so a test can see
        # which unlock a write rode on
        self.events = []

    def pci_interface_id(self) -> int:
        return 0

    def axi_translate(self, path: str):
        import types

        return types.SimpleNamespace(
            addr=SCRATCH_RAM[int(path.split("[")[1].rstrip("]"))]
        )

    def axi_read32(self, addr: int) -> int:
        return self.memory.get(addr, 0)

    def axi_read(self, addr: int, data: bytearray):
        for i in range(0, len(data), 4):
            data[i : i + 4] = self.memory.get(addr + i, 0).to_bytes(4, "little")

    def arc_msg_buf(self, buf, **kwargs):
        code = buf[0] & 0xFF
        self.messages.append(code)
        match code:
            case 0xC2:  # FLASH_UNLOCK
                # Word 1 is what the host claims to have verified
                self.events.append(("unlock", buf[1]))
                return self.unlock_response
            case 0xC3:  # FLASH_LOCK
                self.events.append(("lock", None))
                return [0] * 8
            case _:
                self.events.append(("msg", code))
                return [0] * 8

    def spi_write(self, addr: int, data: bytes):
        self.writes.append((addr, bytes(data)))
        self.events.append(("write", addr))
        # luwen locks the flash again after each write it performs
        self.arc_msg_buf([0xC3, 0, 0, 0, 0, 0, 0, 0])

    def arc_msg(self, *args, **kwargs):
        return (0, 0)


def stage1_bundle(constraints: list) -> "tarfile.TarFile":
    """A bundle holding the three files stage 1 reads for a board."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:

        def add(name: str, payload: bytes):
            info = tarfile.TarInfo(f"./{BOARDNAME}/{name}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        add("image.bin", b"00")
        add("mask.json", json.dumps([]).encode())
        add(
            "compat-variables.json",
            json.dumps({"version": 1, "variables": [spi_eeprom(constraints)]}).encode(),
        )
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


@pytest.fixture()
def stage1(monkeypatch):
    """
    flash_chip_stage1 with the image parsing stubbed out: these tests are
    about the compatibility gate, not about what would have been written.
    """
    from tt_flash import flash as tt_flash_flash
    from tt_flash.chip import BhChip, FwVersion

    monkeypatch.setattr(tt_flash_flash, "parse_writes_from_image", lambda image: [])
    monkeypatch.setattr(tt_flash_flash, "boot_fs_write", lambda *args, **kwargs: [])

    def run(jedec_id: int, constraints: list, unlock_response: list = None, **kwargs):
        chip = BhChip(FakeLuwenChip(jedec_id, unlock_response))
        # Kept so a test whose call raises can still look at the chip
        run.chip = chip
        monkeypatch.setattr(
            chip,
            "get_bundle_version",
            lambda: FwVersion(
                allow_exception=False,
                exception=None,
                running=(19, 14, 99, 0),
                spi=(19, 14, 99, 0),
            ),
        )
        manifest = tt_flash_flash.Manifest(data={}, bundle_version=(19, 14, 100, 0))
        debug_messages = []
        result = tt_flash_flash.flash_chip_stage1(
            chip,
            BOARDNAME,
            manifest,
            stage1_bundle(constraints),
            debug_messages,
            force=False,
            allow_major_downgrades=False,
            **kwargs,
        )
        return chip, result, debug_messages

    return run


def test_stage1_refuses_a_mismatching_bundle(stage1):
    """
    The bundle that started this: a board reporting 0x20bb20 against an image
    that says it supports only 0x4220bb20. The refusal must reach the caller,
    not be swallowed into a result nobody reads.
    """
    with pytest.raises(TTError) as refusal:
        stage1(0x20BB20, [{"eq": "0x4220bb20"}])

    assert "not compatible with this board" in str(refusal.value)
    assert "SPI EEPROM is 0x20bb20" in str(refusal.value)


def test_stage1_accepts_a_matching_bundle(stage1):
    chip, result, debug_messages = stage1(0x20BB20, [{"eq": "0x20bb20"}])

    assert result.state.name == "Ok"
    assert chip.verified_variables == 1
    # The board was asked, and put back the way it was found
    assert chip.luwen_chip.messages == [0xC2, 0xC3]


def test_stage1_reports_every_variable_it_checked(stage1):
    _, _, debug_messages = stage1(0x20BB20, [{"eq": "0x20bb20"}])

    assert any(
        "SPI EEPROM is 0x20bb20, which this firmware supports" in message
        for message in debug_messages
    )


def test_stage1_says_when_a_bundle_declares_nothing(stage1, monkeypatch):
    """
    A bundle with no compatibility data must not look like one whose checks
    all passed.
    """
    from tt_flash import compat_variables as cv

    monkeypatch.setattr(cv, "load", lambda fw_package, boardname: None)

    _, result, debug_messages = stage1(0x20BB20, [{"eq": "0x20bb20"}])

    assert result.state.name == "Ok"
    assert any("does not say which hardware it supports" in m for m in debug_messages)


def test_stage1_refuses_when_firmware_demands_more(stage1):
    """
    Firmware can require a variable this tt-flash did not verify. It refuses
    the unlock, and that refusal has to stop the flash before any write.
    """
    # Status 1, requiring variables 0 and 1. We verified 0; nothing in the
    # bundle describes 1, so we cannot verify it.
    with pytest.raises(TTError) as refusal:
        stage1(
            0x20BB20,
            [{"eq": "0x20bb20"}],
            unlock_response=[1, 0b11, 0, 0, 0, 0, 0, 0],
        )

    assert "will not allow a flash until these are verified" in str(refusal.value)
    assert "board variable 1" in str(refusal.value)
    # Only the variable we could not verify is named
    assert "SPI EEPROM" not in str(refusal.value)

    chip = stage1.chip
    assert chip.luwen_chip.writes == []
    # Refused or not, the board is left locked
    assert chip.luwen_chip.messages == [0xC2, 0xC3]


def test_stage1_mismatch_can_be_forced(stage1):
    chip, result, debug_messages = stage1(
        0x20BB20, [{"eq": "0x4220bb20"}], force_all_variable_checks=True
    )

    assert result.state.name == "Ok"
    # Forced, so firmware is told the check was made, or it would refuse
    assert chip.verified_variables == 1
    assert any("--force-all-variable-checks" in m for m in debug_messages)


def test_every_spi_write_declares_the_variables():
    """
    luwen locks the flash again after each write it performs, which withdraws
    firmware's record of what we verified. A write that is not preceded by its
    own unlock would be refused, so a multi-write flash has to re-declare
    before each one.
    """
    from tt_flash.chip import BhChip

    chip = BhChip(FakeLuwenChip(0x20BB20))
    chip.verified_variables = 0b101

    chip.spi_write(0, b"aaaa")
    chip.spi_write(4, b"bbbb")

    assert chip.luwen_chip.events == [
        ("unlock", 0b101),
        ("write", 0),
        ("lock", None),
        ("unlock", 0b101),
        ("write", 4),
        ("lock", None),
    ]


def test_writes_declare_what_stage1_verified(stage1):
    """
    The declaration a write carries is the result of stage 1's checks, and it
    has to survive the lock stage 1 leaves the board in.
    """
    chip, result, _ = stage1(0x20BB20, [{"eq": "0x20bb20"}])

    chip.spi_write(0x1000, b"aaaa")

    assert chip.luwen_chip.events[-3:] == [
        ("unlock", 1),
        ("write", 0x1000),
        ("lock", None),
    ]
