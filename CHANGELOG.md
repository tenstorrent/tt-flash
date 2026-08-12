# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `flash`: `--update-boot-images` writes the bundle's bootloader and recovery
  images even when the board already holds the same ones, for provisioning and
  board recovery.

### Changed

- `flash`: the boot-critical images (`cmfw`, `safeimg`, `safetail`, `failover`)
  and the ROM and failover descriptor tables are now left alone when the board
  already holds the same content, so a routine update no longer opens a
  power-loss window on the path by which a board boots at all. Whether an image
  is the same is decided by the SHA-256 and key hash that `imgtool` records in
  it, because signing is not reproducible: two builds of the same source differ
  only in the trailing signature. Pass `--update-boot-images` for the previous
  behaviour of writing them unconditionally.

### Fixed

- `flash`: a P300 chip running recovery firmware publishes no board id, so the
  pairing check filed it as not a P300, left its sibling alone in a group of
  one, and dropped both halves of the card from the flash list -- refusing the
  board because of the chip that most needed flashing. Such a chip is now
  identified by its PCI subsystem id, which carries the same UPI whatever the
  chip is running.
- `boot_fs`: `tt_boot_fs_fd.image_tag_str()` compared each `c_uint8` tag byte
  against the string `"\0"`, which never matched, so NUL padding was included in
  the decoded tag. Tags shorter than 8 bytes (e.g. `cmfw`) now compare correctly.
  This makes `read_tag` robust across the multi-table boot filesystem layout
  (ROM, failover, and mutable descriptor tables).

## 3.4.0 - 30/07/25

- Bump pyyaml 6.0.1 -> 6.0.2
- Improve error message formatting
- No longer have to use --force for flashing BH cards

## 3.3.5 - 03/07/25

- Bump luwen 0.7.3 -> 0.7.5

## 3.3.4 - 02/07/25

- Bump tt-tools-common 1.4.16 -> 1.4.17
- Bump luwen 0.6.4 -> 0.7.3

## 3.3.3 - 05/06/2025

- Bumped tt-tools-common version to fix driver version check for compatability with tt-kmd 2.0.0

## 3.3.2 - 14/05/2025

- Bump tt-tools-common version to latest

## 3.2.0 - 12/03/2025

### Updated

- luwen version bump to bring inline with tt-smi; provides stability fixes

## 3.1.3 - 06/03/2025

### Added

- luwen version bump to include bh arc init checks

## 3.1.2 - 28/02/2025

### Added

- Support for more BH cards: p100a, p150, and p150c

## 3.1.1 - 06/01/2025

### Updated

- Bumped luwen version to accomodate Maturin updates

## 3.1.0 - 29/10/2024

### Added

- Support for flashing the BH tt-boot-fs file format
- Bumped luwen version to 0.4.6 to allow resets when chip is inaccessible

## 3.0.2 - 17/10/2024

### Fixed
- Unbound variable when exception is thrown when getting current fw-version

## 3.0.1 - 16/10/2024

### Changed
- Bumped luwen version to 0.4.5 to resolve false positives on bad chip detection

## 3.0.0 - 23/08/2024

- NO BREAKING CHANGES! Major version bump to signify new generation of product.
- Added support for p100

## 2.2.0 - 19/07/2024

### Updated
- Added support for an alternative spi flash configuration via a new version of luwen

## 2.0.8 - 14/05/2024

### Updated
- Bumped luwen (0.3.8) and tt_tools_common (1.4.3) lib versions

## 2.0.1 - 2.0.7
- Dependency updates

## 2.0.0
- WH flash release

## 1.0.0

- GS flash release
