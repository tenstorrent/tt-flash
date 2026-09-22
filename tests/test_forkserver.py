# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tests that run a real pool. These do not require hardware; the workers report
what they inherited instead of flashing anything.

The rest of the suite stands a fake pool in for the real one, which cannot show
what a worker actually starts life with. These start workers for real, so a
worker that quietly depends on the parent's memory shows up here.

Usage:
    pytest tests/test_forkserver.py -v
"""

import pytest

from tt_flash import main, wormhole
from tt_flash.utility import CConfig, CmdLineConfig, pool_worker_init


def report_inherited_state(_) -> tuple:
    """
    Runs in a worker. Reports the state the parent set before the pool started,
    none of which a worker gets unless it is forked from the parent.
    """
    return (
        CConfig.COLOR.use_color,
        CConfig.force_no_tty,
        bytes(wormhole.bundle_version(None, bytearray(4), 0, 0, 4)),
    )


@pytest.fixture()
def parent_state(monkeypatch) -> None:
    """Set every piece of state to something no worker would reach by default."""
    monkeypatch.setattr(CConfig.COLOR, "use_color", False)
    monkeypatch.setattr(CConfig, "force_no_tty", True)
    wormhole.set_bundle_version([19, 6, 1, 0])
    yield
    wormhole.set_bundle_version([0xFF, 0xFF, 0xFF, 0xFF])


def test_flash_pool_uses_forkserver():
    # Pool is bound to the context it came from.
    assert main.Pool.__self__.get_start_method() == "forkserver"


def test_a_worker_inherits_nothing_from_the_parent(parent_state):
    """
    The control the other tests rest on. If this ever starts seeing the
    parent's values, workers are being forked and the rest of this file proves
    nothing.
    """
    with main.Pool(processes=1) as p:
        use_color, force_no_tty, version = p.map(report_inherited_state, [0])[0]

    assert (use_color, force_no_tty) == (True, False)
    assert version == b"\xff\xff\xff\xff"


def test_the_initializer_gives_a_worker_the_config(parent_state):
    """The config has to survive being pickled over to the worker."""
    config = CmdLineConfig(use_color=False, force_no_tty=True)

    with main.Pool(
        processes=1, initializer=pool_worker_init, initargs=(config,)
    ) as p:
        use_color, force_no_tty, _ = p.map(report_inherited_state, [0])[0]

    assert (use_color, force_no_tty) == (False, True)
