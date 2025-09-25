#!/bin/bash

# Simple tt-flash binary compilation script
# Usage: ./compile.sh

set -e  # Exit on any error

# Color definitions
PURPLE='\033[0;35m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to check and install PyInstaller if needed
check_pyinstaller() {
    if ! command -v pyinstaller &> /dev/null && ! command -v python3 -m PyInstaller &> /dev/null; then
        echo -e "${PURPLE}[INFO] PyInstaller not found. Installing...${NC}"
        pip3 install pyinstaller
    fi
}

# Function to clean previous build artifacts
clean_build() {
    echo -e "${PURPLE}[INFO] Cleaning previous build artifacts...${NC}"
    rm -rf build/ dist/ *.spec
}

# Function to build the binary using PyInstaller
build_binary() {
    echo -e "${PURPLE}[INFO] Building binary with PyInstaller...${NC}"
    
    # Try pyinstaller command first, then fall back to python3 -m PyInstaller
    if command -v pyinstaller &> /dev/null; then
        PYINSTALLER_CMD="pyinstaller"
    else
        PYINSTALLER_CMD="python3 -m PyInstaller"
    fi

    $PYINSTALLER_CMD \
        --onefile \
        --name tt-flash \
        --add-data "tt_flash/data:tt_flash/data" \
        --add-data "pyproject.toml:." \
        --hidden-import tt_flash \
        --hidden-import pyluwen \
        --hidden-import tt_tools_common \
        --hidden-import importlib_metadata \
        --hidden-import importlib_resources \
        --clean \
        tt_flash_entry.py
}

# Function to verify compilation success
verify_build() {
    if [ -f "dist/tt-flash" ]; then
        echo -e "${GREEN}[INFO] Binary compilation completed successfully${NC}"
        return 0
    else
        echo -e "${RED}[ERROR] Binary creation failed${NC}"
        return 1
    fi
}

# function to rename tt-flash to tt-flash-<version>
rename_binary() {
    echo -e "${PURPLE}[INFO] Renaming binary to tt-flash-${DATE}-${VERSION}${NC}"
    mv dist/tt-flash dist/tt-flash-${DATE}-${VERSION}
}

# Function to display completion message
show_completion() {
    echo ""
    echo -e "${PURPLE}[INFO] tt-flash binary is ready for deployment${NC}"
    echo -e "Binary location: ${PWD}/dist/tt-flash-${DATE}-${VERSION}"
    echo -e "Binary size: $(du -sh dist/tt-flash-${DATE}-${VERSION} | cut -f1)"
}

# Main function to orchestrate the compilation process
main() {
    # Get version from git commit hash
    VERSION=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    DATE=$(date +%Y-%m-%d)
    
    echo -e "${PURPLE}[INFO] Initiating tt-flash binary compilation...${NC}"
    echo -e "${PURPLE}[INFO] Version: ${VERSION}${NC}"
    
    check_pyinstaller
    clean_build
    build_binary
    verify_build
    rename_binary
    show_completion
}

# Run main function
main "$@"
