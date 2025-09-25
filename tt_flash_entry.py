#!/usr/bin/env python3
"""
Entry point script for tt-flash binary compilation.
This script handles the import issues that can occur with PyInstaller.
"""

import sys
import os
from pathlib import Path

# Add the current directory to Python path to ensure imports work
current_dir = Path(__file__).parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

# Also add the tt_flash directory
tt_flash_dir = current_dir / 'tt_flash'
if tt_flash_dir.exists() and str(tt_flash_dir) not in sys.path:
    sys.path.insert(0, str(tt_flash_dir))

def main():
    """Main entry point for the binary."""
    try:
        # Import and run the main function
        from tt_flash.main import main as tt_flash_main
        return tt_flash_main()
    except ImportError as e:
        print(f"Import error: {e}")
        print("Please ensure all dependencies are properly installed.")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
