#!/bin/bash

# Simple tt-flash binary compilation script
# Usage: ./compile.sh

set -e  # Exit on any error

# Function to check and install PyInstaller if needed
check_pyinstaller() {
    if ! command -v pyinstaller &> /dev/null && ! command -v python3 -m PyInstaller &> /dev/null; then
        echo "📦 PyInstaller not found. Installing..."
        pip3 install pyinstaller
    fi
}

# Function to clean previous build artifacts
clean_build() {
    echo "🧹 Cleaning previous build..."
    rm -rf build/ dist/ *.spec
}

# Function to build the binary using PyInstaller
build_binary() {
    echo "⚙️  Building binary..."

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
        echo "✅ Compilation successful!"
        return 0
    else
        echo "❌ Binary creation failed!"
        return 1
    fi
}

# Function to display completion message
show_completion() {
    echo ""
    echo "🎉 Done! Your tt-flash binary is ready to use."
}

# Main function to orchestrate the compilation process
main() {
    echo "🔨 Compiling tt-flash to binary..."

    check_pyinstaller
    clean_build
    build_binary
    verify_build
    show_completion
}

# Run main function
main "$@"
