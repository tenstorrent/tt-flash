# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for package version lookup and fallback."""

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import tt_flash
import tt_flash.__init__ as tt_flash_init


class TestPackageVersion(unittest.TestCase):
    def test_package_version_source_checkout_fallback(self):
        """Verify that tt_flash.__version__ falls back cleanly without NameError."""
        tt_flash_init.__package_version = "unknown"
        version = tt_flash.__version__
        self.assertIsInstance(version, str)
        self.assertTrue(len(version) > 0)
        self.assertTrue(version.endswith("+"))

    def test_package_version_handles_package_not_found(self):
        """Verify that PackageNotFoundError from importlib_metadata is caught."""
        tt_flash_init.__package_version = "unknown"

        def mock_version(pkg_name):
            raise tt_flash_init.importlib_metadata.PackageNotFoundError(pkg_name)

        with patch.object(tt_flash_init.importlib_metadata, "version", side_effect=mock_version):
            version = getattr(tt_flash_init, "__get_package_version")()
            self.assertIsInstance(version, str)
            self.assertTrue(version.endswith("+"))


if __name__ == "__main__":
    unittest.main()
