# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the no-retimer Galaxy IPMI reset used when flashing a GLX."""

from unittest.mock import MagicMock, patch

import pytest

from tt_flash.error import TTError
from tt_flash.flash import (
    GLX_IPMI_OP_MODE_NO_RETIMER,
    GLX_IPMI_OP_MODE_WITH_RETIMER,
    run_galaxy_ipmi_reset,
)

BMC_C9_STDERR = (
    "Unable to send RAW command (channel=0x0 netfn=0x30 lun=0x0 cmd=0x8b rsp=0xc9): "
    "Parameter out of range"
)


class TestGalaxyIpmiReset:
    def test_issues_op_mode_no_retimer(self):
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("tt_flash.flash.subprocess.run", return_value=mock_result) as mock_run:
            run_galaxy_ipmi_reset()
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == [
            "sudo",
            "ipmitool",
            "raw",
            "0x30",
            "0x8B",
            "0xF",
            "0xFF",
            GLX_IPMI_OP_MODE_NO_RETIMER,
            "0xF",
        ]

    def test_c9_falls_back_to_legacy_retimer_reset(self):
        fail_new = MagicMock(returncode=1, stdout="", stderr=BMC_C9_STDERR)
        ok_old = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "tt_flash.flash.subprocess.run", side_effect=[fail_new, ok_old]
        ) as mock_run:
            run_galaxy_ipmi_reset()
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0][7] == GLX_IPMI_OP_MODE_NO_RETIMER
        assert mock_run.call_args_list[1][0][0][7] == GLX_IPMI_OP_MODE_WITH_RETIMER

    def test_other_ipmi_failure_falls_back_to_legacy_retimer_reset(self):
        fail_new = MagicMock(returncode=1, stdout="", stderr="Could not open device")
        ok_old = MagicMock(returncode=0, stdout="", stderr="")
        with patch(
            "tt_flash.flash.subprocess.run", side_effect=[fail_new, ok_old]
        ) as mock_run:
            run_galaxy_ipmi_reset()
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1][0][0][7] == GLX_IPMI_OP_MODE_WITH_RETIMER

    def test_fallback_failure_raises(self):
        fail_new = MagicMock(returncode=1, stdout="", stderr=BMC_C9_STDERR)
        fail_old = MagicMock(returncode=1, stdout="", stderr="Could not open device")
        with patch("tt_flash.flash.subprocess.run", side_effect=[fail_new, fail_old]):
            with pytest.raises(TTError, match="IPMI command failed"):
                run_galaxy_ipmi_reset()

    def test_missing_ipmitool_raises(self):
        with patch(
            "tt_flash.flash.subprocess.run", side_effect=FileNotFoundError("sudo")
        ):
            with pytest.raises(TTError, match="ipmitool"):
                run_galaxy_ipmi_reset()

    def test_legacy_op_mode_does_not_retry(self):
        fail_old = MagicMock(returncode=1, stdout="", stderr="Could not open device")
        with patch("tt_flash.flash.subprocess.run", return_value=fail_old) as mock_run:
            with pytest.raises(TTError, match="IPMI command failed"):
                run_galaxy_ipmi_reset(op_mode=GLX_IPMI_OP_MODE_WITH_RETIMER)
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][7] == GLX_IPMI_OP_MODE_WITH_RETIMER
