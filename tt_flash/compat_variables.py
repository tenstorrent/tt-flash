# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Board variables: hardware attributes that decide, together with the board type,
whether a firmware image is compatible with the board it is about to be written
to.

Each image in a firmware bundle carries a compat-variables.json naming the
variables it constrains, how to read each one from the board, and the values it
permits. We evaluate those here and report which variables we managed to
verify; firmware refuses to unlock the flash unless the variables that matter on
the board in front of us are among them.

Nothing here is specific to a particular variable. A variable whose value comes
from a telemetry tag needs no code change, which is the point: firmware can
start requiring a new variable and an unmodified tt-flash still honours it, so
long as the bundle describes it. Anything we do not understand is left
unverified rather than guessed at, so we fail closed: firmware then decides
whether that variable mattered on this board.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
import json
import tarfile
from typing import Any, Callable, Optional

from tt_flash.error import TTError

# The compat-variables.json format version we understand
SUPPORTED_VERSION = 1

def _format_hex(value: int) -> str:
    return f"0x{value:x}"


def _format_semver(value: int) -> str:
    return f"{(value >> 24) & 0xFF}.{(value >> 16) & 0xFF}.{(value >> 8) & 0xFF}"


# A formatter only decides how a value is printed, so an unknown one falls back
# to hex rather than leaving the variable unverified: a newer bundle must not
# stop an older tt-flash from doing its job, or even from explaining itself.
FORMATTERS: dict[str, Callable[[int], str]] = {
    "hex": _format_hex,
    "semver": _format_semver,
}

# Every operator present in a constraint must hold
OPERATORS: dict[str, Callable[[int, Any], bool]] = {
    "eq": lambda value, want: value == want,
    "ne": lambda value, want: value != want,
    "lt": lambda value, want: value < want,
    "le": lambda value, want: value <= want,
    "gt": lambda value, want: value > want,
    "ge": lambda value, want: value >= want,
    "in": lambda value, want: value in want,
    "not_in": lambda value, want: value not in want,
}


def _parse_value(value: Any) -> int:
    """
    Board variable values are unsigned 32 bit integers, written either as a JSON
    integer or as a "0x..." string.
    """

    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value, 0)
        except ValueError:
            raise TTError(f"Invalid board variable value {value!r}")
    else:
        raise TTError(f"Invalid board variable value {value!r}")

    if not 0 <= parsed < 2**32:
        raise TTError(
            f"Board variable value {value!r} is not a 32 bit unsigned integer"
        )

    return parsed


@dataclass
class Variable:
    name: str
    number: int
    formatter: Optional[str] = None
    source: Optional[dict] = None
    # The comparisons the value must satisfy, as (operator, operand) pairs, all
    # of which must hold. Empty permits any value. None when the image
    # constrains this variable in a way we cannot evaluate, which leaves it
    # unverified.
    constraints: Optional[list[tuple[str, int | list[int]]]] = None

    @staticmethod
    def loads(data: dict) -> Variable:
        try:
            variable = Variable(
                name=data["name"],
                number=data["number"],
                formatter=data.get("formatter"),
                source=data.get("source"),
            )
            constraints = data["constraints"]
        except KeyError as e:
            raise TTError(f"Board variable is missing {e} in compat-variables.json")

        parsed: list[tuple[str, int | list[int]]] = []
        for constraint in constraints:
            for operator, operand in constraint.items():
                if operator not in OPERATORS:
                    # A newer bundle's operator. Leave constraints unset so the
                    # variable stays unverified and firmware decides if it
                    # mattered.
                    variable.constraints = None
                    return variable

                if isinstance(operand, list):
                    parsed.append((operator, [_parse_value(v) for v in operand]))
                else:
                    parsed.append((operator, _parse_value(operand)))

        variable.constraints = parsed
        return variable

    def format(self, value: int) -> str:
        if self.formatter is None:
            return _format_hex(value)

        formatter = FORMATTERS.get(self.formatter, _format_hex)
        return formatter(value)

    def describe_constraint(self) -> str:
        """
        The permitted values, phrased for the user whose board does not match.
        """

        if self.constraints is None:
            return "values this tt-flash does not understand"

        described = []
        for operator, operand in self.constraints:
            if isinstance(operand, list):
                values = ", ".join(self.format(v) for v in operand)
            else:
                values = self.format(operand)

            if operator == "eq":
                described.append(values)
            elif operator == "in":
                described.append(f"one of {values}")
            else:
                described.append(f"{operator} {values}")

        if not described:
            return "any value"

        return " and ".join(described)

    def satisfied_by(self, value: int) -> bool:
        """
        Whether the value meets every constraint. False when the image
        constrains this variable in a way we cannot evaluate: not a verdict
        that the board is unsupported, only a refusal to claim it is.
        """

        if self.constraints is None:
            return False

        return all(
            OPERATORS[operator](value, operand)
            for operator, operand in self.constraints
        )


@dataclass
class CompatVariables:
    variables: list[Variable] = field(default_factory=list)
    # Why we could not evaluate parts of the file, for the message we print if
    # firmware then refuses to unlock
    unsupported: list[str] = field(default_factory=list)

    @staticmethod
    def loads(text: str) -> CompatVariables:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise TTError(f"Could not parse compat-variables.json: {e}")

        version = data.get("version")
        if version != SUPPORTED_VERSION:
            # A format we don't know. Verify nothing and let firmware decide
            # whether that mattered, rather than refusing outright on a board
            # that needs none of this.
            return CompatVariables(
                unsupported=[
                    f"compat-variables.json is version {version}, and this "
                    f"tt-flash understands version {SUPPORTED_VERSION}"
                ]
            )

        compat = CompatVariables()
        for entry in data.get("variables", []):
            variable = Variable.loads(entry)
            compat.variables.append(variable)
            if variable.constraints is None:
                compat.unsupported.append(
                    f"'{variable.name}' is constrained in a way this tt-flash "
                    f"does not understand"
                )

        return compat

    def name_of(self, number: int) -> str:
        for variable in self.variables:
            if variable.number == number:
                return variable.name

        return f"board variable {number}"


def load(fw_package: tarfile.TarFile, boardname: str) -> Optional[CompatVariables]:
    """
    Reads the compat variables for one board out of a firmware bundle.

    Bundles built before board variables existed have no such file. They
    describe no variables, so they verify none, which is what leaves them unable
    to unlock a board that requires any.
    """

    try:
        contents = fw_package.extractfile(f"./{boardname}/compat-variables.json")
    except KeyError:
        return None

    if contents is None:
        return None

    return CompatVariables.loads(contents.read().decode("utf-8"))


def read_variable(chip, variable: Variable) -> Optional[int]:
    """
    Reads a board variable's value, or None if this tt-flash cannot.
    """

    source = variable.source
    if source is None:
        return None

    if source.get("type") != "telemetry":
        # A retrieval method we don't know about. Guessing would defeat the
        # point of the check.
        return None

    tag = source.get("tag")
    if tag is None:
        return None

    return chip.read_telemetry_tag(tag)


class CheckState(Enum):
    """What we were able to conclude about one board variable."""

    #: read from the board, and the image supports it
    PASSED = auto()
    #: read from the board, and the image does not support it
    FAILED = auto()
    #: not read, so nothing was concluded either way
    UNCHECKED = auto()


@dataclass
class Check:
    """
    The outcome of checking one board variable, and how to say it.

    Every variable the image constrains produces one of these, passing or
    not, so that a caller can report what it actually concluded rather than
    leaving silence to stand for success.
    """

    variable: Variable
    value: Optional[int]
    state: CheckState
    message: str


def failed(checks: list[Check]) -> list[Check]:
    return [check for check in checks if check.state is CheckState.FAILED]


def evaluate(chip, compat: Optional[CompatVariables]) -> tuple[int, list[Check]]:
    """
    Checks each variable the image constrains against the board.

    Returns the bitmask of variables verified against this image, where bit N is
    variable N, and a Check for every variable the image constrains. A variable
    we cannot read, or cannot evaluate, comes back UNCHECKED and unverified.
    """

    verified = 0
    checks: list[Check] = []

    if compat is None:
        return verified, checks

    for variable in compat.variables:
        if variable.constraints is None:
            checks.append(
                Check(
                    variable=variable,
                    value=None,
                    state=CheckState.UNCHECKED,
                    message=(
                        f"{variable.name} is constrained in a way this tt-flash "
                        f"does not understand, so it was not checked"
                    ),
                )
            )
            continue

        value = read_variable(chip, variable)
        if value is None:
            checks.append(
                Check(
                    variable=variable,
                    value=None,
                    state=CheckState.UNCHECKED,
                    message=(
                        f"{variable.name} could not be read from this board, so "
                        f"it was not checked"
                    ),
                )
            )
            continue

        if variable.satisfied_by(value):
            verified |= 1 << variable.number
            checks.append(
                Check(
                    variable=variable,
                    value=value,
                    state=CheckState.PASSED,
                    message=(
                        f"{variable.name} is {variable.format(value)}, which this "
                        f"firmware supports"
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    variable=variable,
                    value=value,
                    state=CheckState.FAILED,
                    message=(
                        f"{variable.name} is {variable.format(value)}; "
                        f"this firmware supports {variable.describe_constraint()}"
                    ),
                )
            )

    return verified, checks


def describe_missing(
    required: int, verified: int, compat: Optional[CompatVariables]
) -> str:
    """
    Explains a refused FLASH_UNLOCK: which board variables the firmware needed
    checked that we could not check.
    """

    missing = required & ~verified
    names = [
        compat.name_of(bit) if compat is not None else f"board variable {bit}"
        for bit in range(32)
        if missing & (1 << bit)
    ]

    message = (
        "The firmware on this board will not allow a flash until these are "
        f"verified against the image: {', '.join(names)}."
    )

    if compat is not None and compat.unsupported:
        message += " " + " ".join(compat.unsupported) + "."
    elif compat is None:
        message += (
            " This firmware bundle does not say which hardware it supports; "
            "use a newer bundle."
        )
    else:
        message += " Use a newer tt-flash or a newer firmware bundle."

    return message
