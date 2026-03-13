# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import requests

from tt_flash.error import TTError

CACHE_DIR = Path.home() / ".cache" / "tt-flash"


def download_fwbundle(version: str, no_tty: bool = False) -> Path:
    """Download a firmware bundle from the tt-system-firmware GitHub releases.

    Args:
        version: "latest" or a specific version string (e.g. "19.6.0").

    Returns:
        Path to the downloaded .fwbundle file (cached in ~/.cache/tt-flash/).
    """
    if version == "latest":
        release_url = f"https://api.github.com/repos/tenstorrent/tt-system-firmware/releases/latest"
    else:
        tag = version if version.startswith("v") else f"v{version}"
        release_url = f"https://api.github.com/repos/tenstorrent/tt-system-firmware/releases/tags/{tag}"

    response = requests.get(release_url, timeout=30)
    if response.status_code == 404:
        raise TTError(f"Firmware release '{version}' not found.")
    response.raise_for_status()

    release = response.json()
    release_version = release["tag_name"].lstrip("v")
    asset_name = f"fw_pack-{release_version}.fwbundle"

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached_path = CACHE_DIR / asset_name
    if cached_path.exists():
        print(f"\tUsing cached {asset_name}")
        return cached_path

    asset_url = next(
        (a["browser_download_url"] for a in release.get("assets", []) if a["name"] == asset_name),
        None,
    )
    if asset_url is None:
        raise TTError(f"Asset '{asset_name}' not found in release {release['tag_name']}")

    dl = requests.get(asset_url, stream=True, timeout=120)
    dl.raise_for_status()

    total = int(dl.headers.get("content-length", 0))
    downloaded = 0
    partial_path = cached_path.with_suffix(".fwbundle.partial")

    if no_tty:
        print(f"\tDownloading {asset_name}...")
    try:
        with open(partial_path, "wb") as f:
            for chunk in dl.iter_content(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    if not no_tty:
                        print(f"\r\tDownloading {asset_name}... {pct:3d}%  {downloaded // 1024} / {total // 1024} KB", end="", flush=True)
        if total and no_tty:
            print(f"\t{pct:3d}%  {downloaded // 1024} / {total // 1024} KB")
        else:
            print()  # newline after progress
        partial_path.rename(cached_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    return cached_path
